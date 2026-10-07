"""Presentation for the maintained 1D consumer; physical results stay immutable."""

from dataclasses import dataclass
from pathlib import Path

from .scan_1d import finite


@dataclass(frozen=True)
class DisplayConfig:
    scales: tuple = ()
    labels: tuple = ()
    ranges: tuple = ()
    title: str = ""
    luminosity: str = ""
    collision_energy: str = ""
    reference: str = ""
    output_directory: str = "."
    formats: tuple = ("png", "pdf")

    @classmethod
    def from_mapping(cls, values=None):
        values = dict(values or {})
        unknown = set(values) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError("unknown display options: {}".format(sorted(unknown)))
        for key in ("scales", "labels", "ranges"):
            values[key] = tuple(sorted(dict(values.get(key, {})).items()))
        values["scales"] = tuple((wc, finite(scale, "display scale")) for wc, scale in values["scales"])
        if any(scale <= 0 for _, scale in values["scales"]):
            raise ValueError("display scales must be positive")
        ranges = []
        for wc, bounds in values["ranges"]:
            if len(bounds) != 2:
                raise ValueError("display range requires two physical coordinates")
            low, high = (finite(value, "display range") for value in bounds)
            if low >= high:
                raise ValueError("display range must increase")
            ranges.append((wc, (low, high)))
        values["ranges"] = tuple(ranges)
        values["formats"] = tuple(values.get("formats", ("png", "pdf")))
        if not values["formats"] or set(values["formats"]) - {"png", "pdf", "eps", "svg"}:
            raise ValueError("unsupported or empty plot formats")
        return cls(**values)

    def scale(self, wc):
        return dict(self.scales).get(wc, 1.0)

    def label(self, wc):
        label = dict(self.labels).get(wc, wc)
        scale = self.scale(wc)
        return label if scale == 1 else "{} ×{:g}".format(label, scale)

    @property
    def annotation(self):
        return " | ".join(value for value in (self.title, self.luminosity, self.collision_energy, self.reference) if value)


def _figure(size):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    figure = Figure(figsize=size)
    FigureCanvasAgg(figure)
    return figure, figure.subplots()


def _save(figure, name, display):
    directory = Path(display.output_directory)
    if not directory.is_dir():
        raise ValueError("plot output directory must already exist: {}".format(directory))
    if not name or Path(name).name != name:
        raise ValueError("plot filename must be a basename")
    paths = tuple(directory / (name + "." + extension) for extension in display.formats)
    if any(path.exists() for path in paths):
        raise ValueError("plot output already exists")
    figure.tight_layout()
    for path in paths:
        figure.savefig(path)
    return tuple(str(path) for path in paths)


def render_curves(scans, titles, display, name, *, thresholds=(1, 4), ceiling=12, log=False):
    if not scans or len(scans) != len(titles) or len({scan.wc for scan in scans}) != 1:
        raise ValueError("one title per scan and a common WC are required")
    ceiling = finite(ceiling, "plot ceiling")
    thresholds = tuple(finite(value, "asymptotic reference threshold") for value in thresholds)
    if ceiling <= 0 or any(value < 0 for value in thresholds):
        raise ValueError("invalid plot ceiling or threshold")
    wc = scans[0].wc
    scale = display.scale(wc)
    physical_extent = dict(display.ranges).get(wc)
    if physical_extent is None:
        coordinates = [point.coordinate for scan in scans for point in scan.grid_points]
        physical_extent = (min(coordinates), max(coordinates))
    if physical_extent[0] == physical_extent[1]:
        raise ValueError("insufficient scan extent for rendering")
    extent = tuple(value * scale for value in physical_extent)
    figure, axes = _figure((8, 5.5))
    for scan, title in zip(scans, titles):
        axes.plot([point.coordinate * scale for point in scan.curve_points],
                  [point.twice_delta_nll for point in scan.curve_points], ".-", label=title)
    for threshold in thresholds:
        axes.axhline(threshold, linestyle="--", linewidth=1, label="Asymptotic reference: {:g}".format(threshold))
    axes.set(xlim=extent, ylim=(0.01 if log else 0, ceiling), xlabel=display.label(wc),
             ylabel="2ΔNLL (minimum of supplied entries subtracted)", title=display.annotation)
    if log:
        axes.set_yscale("log")
    axes.grid(alpha=0.3)
    axes.legend(fontsize="small")
    return {"paths": _save(figure, name, display), "display_extent": extent}


def render_summary(rows, titles, display, name, asimov=False):
    """Render every component at thresholds 1 and 4, with arrows for open ends."""
    if not rows or len(titles) != 2:
        raise ValueError("summary requires rows and two family titles")
    figure, axes = _figure((10, max(4, 0.65 * len(rows) + 2)))
    labels = []
    for index, (wc, profiled, frozen) in enumerate(rows):
        scale = display.scale(wc)
        labels.append(display.label(wc))
        for family, scan in enumerate((profiled, frozen)):
            position = index + (-0.13 if family == 0 else 0.13)
            color = "black" if family == 0 else "tab:red"
            for threshold, width in ((4, 2), (1, 5)):
                result = scan.intervals(threshold)
                if not result.components:
                    raise ValueError("{}: summary interval {} is {}".format(wc, threshold, result.status))
                for component in result.components:
                    left = component.lower.value
                    right = component.upper.value
                    axes.plot([scale * (result.domain[0] if left is None else left),
                               scale * (result.domain[1] if right is None else right)],
                              [position, position], color=color, linewidth=width,
                              label="{}; asymptotic reference {}".format(titles[family], threshold) if index == 0 and component is result.components[0] else None)
                    for bound, edge, marker in ((left, result.domain[0], "<"), (right, result.domain[1], ">")):
                        if bound is None:
                            axes.plot(scale * edge, position, marker=marker, color=color, markersize=9)
            axes.plot([value * scale for value in scan.minima], [position] * len(scan.minima),
                      linestyle="none", marker="o" if asimov else "D", color=color, markersize=4)
    axes.set_yticks(range(len(rows)))
    axes.set_yticklabels(labels)
    axes.set(xlabel="Coefficient (display scale annotated per row)", title=display.annotation)
    axes.grid(axis="x", alpha=0.3)
    axes.legend(fontsize="small")
    axes.text(0, -0.14, "Arrows: open at scan boundary; reference thresholds do not establish coverage.", transform=axes.transAxes, fontsize=8)
    return {"paths": _save(figure, name, display)}

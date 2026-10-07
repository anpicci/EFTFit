"""Validated MultiDimFit grid results and piecewise-linear reference intervals.

No analysis constants, ROOT runtime, plotting state, or fit execution lives here.
The input contract is one observed/Asimov grid result, not a mixture of toys.
"""

from dataclasses import dataclass
import math
import warnings


def finite(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("{} must be a finite scalar".format(label)) from error
    if not math.isfinite(result):
        raise ValueError("{} must be finite".format(label))
    return result


@dataclass(frozen=True)
class ScanEntry:
    coordinate: float
    delta_nll: float
    kind: str
    tracked_parameters: tuple = ()
    locator: str = ""


@dataclass(frozen=True)
class ScanPoint:
    coordinate: float
    delta_nll: float
    twice_delta_nll: float
    entries: tuple


@dataclass(frozen=True)
class ScanResult:
    wc: str
    raw_entries: tuple
    grid_entries: tuple
    special_entries: tuple
    points: tuple
    grid_points: tuple
    normalization_delta_nll: float
    duplicate_coordinates: tuple
    provenance: tuple
    classification: str

    @property
    def sampled_grid_minima(self):
        value = min(point.delta_nll for point in self.grid_points)
        return tuple(point.coordinate for point in self.grid_points if point.delta_nll == value)

    @property
    def reported_fit_minima(self):
        """Special-entry fit estimates; these do not certify a global minimum."""
        if not self.special_entries:
            return ()
        value = min(entry.delta_nll for entry in self.special_entries)
        return tuple(sorted({entry.coordinate for entry in self.special_entries if entry.delta_nll == value}))

    @property
    def minima(self):
        return tuple(point.coordinate for point in self.points if point.twice_delta_nll == 0)

    @property
    def curve_points(self):
        """Canonical likelihood at grid coordinates, including coincident fit rows.

        Off-grid special entries set the normalization but do not manufacture
        extra interval-resolution knots. Raw sampled minima remain separate.
        """
        coordinates = {point.coordinate for point in self.grid_points}
        return tuple(point for point in self.points if point.coordinate in coordinates)

    def intervals(self, threshold):
        return extract_intervals(
            [point.coordinate for point in self.curve_points],
            [point.twice_delta_nll for point in self.curve_points], threshold,
        )


def canonicalize_entries(wc, entries, provenance=(), classification="explicit"):
    if not isinstance(wc, str) or not wc:
        raise ValueError("scan WC name is required")
    entries = tuple(entries)
    if not entries:
        raise ValueError("empty scan result")
    checked = []
    for entry in entries:
        if entry.kind not in {"grid", "special_fit", "unclassified"}:
            raise ValueError("unsupported entry kind: {}".format(entry.kind))
        checked.append(ScanEntry(
            finite(entry.coordinate, entry.locator + ": coordinate"),
            finite(entry.delta_nll, entry.locator + ": deltaNLL"), entry.kind,
            tuple((key, finite(value, key)) for key, value in entry.tracked_parameters),
            entry.locator,
        ))
    entries = tuple(checked)
    grid = tuple(entry for entry in entries if entry.kind != "special_fit")
    special = tuple(entry for entry in entries if entry.kind == "special_fit")
    if not grid:
        raise ValueError("result contains no grid entries")
    baseline = min(entry.delta_nll for entry in entries)

    def points_for(selected):
        grouped = {}
        for entry in selected:
            grouped.setdefault(entry.coordinate, []).append(entry)
        return tuple(ScanPoint(
            coordinate, min(entry.delta_nll for entry in group),
            finite(2 * (min(entry.delta_nll for entry in group) - baseline), "normalized 2*deltaNLL"),
            tuple(sorted(group, key=lambda entry: (entry.delta_nll, entry.kind, entry.tracked_parameters, entry.locator))),
        ) for coordinate, group in sorted(grouped.items()))

    points = points_for(entries)
    return ScanResult(wc, entries, grid, special, points, points_for(grid), baseline,
                      tuple((point.coordinate, len(point.entries)) for point in points if len(point.entries) > 1),
                      tuple(provenance), classification)


def scan_from_columns(columns, wc, locator="memory"):
    for name in (wc, "deltaNLL"):
        if name not in columns:
            raise ValueError("{}: missing required branch {}".format(locator, name))
    size = len(columns[wc])
    if not size:
        raise ValueError("{}: empty result".format(locator))
    if any(len(values) != size for values in columns.values()):
        raise ValueError("{}: unequal branch lengths".format(locator))
    if "iToy" in columns and len(set(columns["iToy"])) != 1:
        raise ValueError("{}: mixed toys cannot form one likelihood curve".format(locator))
    has_quantile = "quantileExpected" in columns
    classification = "combine_grid_quantile" if has_quantile else "unclassified_as_grid"
    if not has_quantile:
        warnings.warn("{}: no quantileExpected; entries are unclassified, treated as grid; no continuous fit minimum inferred".format(locator), RuntimeWarning)
    entries = []
    tracked = sorted(key for key in columns if key.startswith("trackedParam_"))
    for index in range(size):
        entry_locator = "{}:limit[{}]".format(locator, index)
        quantile = finite(columns["quantileExpected"][index], entry_locator + ": quantileExpected") if has_quantile else None
        if quantile is not None and quantile < 0 and quantile != -1:
            raise ValueError("{}: unsupported negative quantile for grid result".format(entry_locator))
        kind = "special_fit" if quantile == -1 else ("grid" if has_quantile else "unclassified")
        entries.append(ScanEntry(columns[wc][index], columns["deltaNLL"][index], kind,
                                 tuple((key, columns[key][index]) for key in tracked), entry_locator))
    return canonicalize_entries(wc, entries, (str(locator),), classification)


def read_scan(path, wc):
    """Read a local ROOT limit TTree with uproot; never execute result content."""
    import uproot
    try:
        with uproot.open(path) as source:
            if "limit" not in source:
                raise ValueError("missing limit tree")
            tree = source["limit"]
            if not hasattr(tree, "arrays"):
                raise ValueError("limit object is not a tree")
            names = set(tree.keys())
            for name in (wc, "deltaNLL"):
                if name not in names:
                    raise ValueError("missing required branch {}".format(name))
            selected = names & {wc, "deltaNLL", "quantileExpected", "iToy"}
            selected.update(name for name in names if name.startswith("trackedParam_"))
            columns = tree.arrays(sorted(selected), library="np", how=dict)
            return scan_from_columns(columns, wc, str(path))
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError("{}: invalid 1D result: {}".format(path, error)) from error


def combine_scans(scans, common_reference=False):
    """Combine shards only with caller-certified common raw deltaNLL reference."""
    scans = tuple(scans)
    if not scans:
        raise ValueError("empty input family")
    if len({scan.wc for scan in scans}) != 1:
        raise ValueError("input family mixes scan WCs")
    if len(scans) > 1 and common_reference is not True:
        raise ValueError("multiple results require common_reference=True after verifying their likelihood reference")
    if len(scans) == 1:
        return scans[0]
    return canonicalize_entries(scans[0].wc,
                               (entry for scan in scans for entry in scan.raw_entries),
                               (path for scan in scans for path in scan.provenance),
                               "combined_common_reference")


@dataclass(frozen=True)
class Boundary:
    value: object
    kind: str
    bracket: tuple


@dataclass(frozen=True)
class Interval:
    lower: Boundary
    upper: Boundary

    @property
    def bounded(self):
        return self.lower.value is not None and self.upper.value is not None


@dataclass(frozen=True)
class IntervalResult:
    threshold: float
    components: tuple
    domain: tuple
    local_minima: tuple
    status: str
    semantics: str = "asymptotic_reference"


def extract_intervals(x, twice_delta_nll, threshold):
    """Sublevel components of the piecewise-linear grid, without extrapolation.

    None endpoints mean scan-boundary censoring, not infinite physical limits.
    A bracket records the sampled coordinates supporting each crossing.
    """
    x = tuple(finite(value, "coordinate") for value in x)
    y = tuple(finite(value, "2*deltaNLL") for value in twice_delta_nll)
    threshold = finite(threshold, "threshold")
    if not x or len(x) != len(y):
        raise ValueError("nonempty equal-length coordinate and likelihood arrays required")
    if threshold < 0 or any(value < 0 for value in y):
        raise ValueError("threshold and normalized 2*deltaNLL must be nonnegative")
    if any(right <= left for left, right in zip(x, x[1:])):
        raise ValueError("coordinates must be sorted and unique")
    minima = tuple(x[index] for index, value in enumerate(y)
                   if (index == 0 or value <= y[index - 1])
                   and (index == len(y) - 1 or value <= y[index + 1]))
    if len(x) < 2:
        return IntervalResult(threshold, (), (x[0], x[-1]), minima, "insufficient_resolution")

    def crossing(index):
        left_x, right_x = x[index], x[index + 1]
        left_y, right_y = y[index], y[index + 1]
        if left_y == threshold:
            return Boundary(left_x, "sampled_threshold", (left_x, left_x))
        if right_y == threshold:
            return Boundary(right_x, "sampled_threshold", (right_x, right_x))
        if left_y == right_y or not min(left_y, right_y) < threshold < max(left_y, right_y):
            raise ValueError("crossing is not bracketed by a non-degenerate segment")
        fraction = (threshold - left_y) / (right_y - left_y)
        value = finite((1 - fraction) * left_x + fraction * right_x, "crossing")
        return Boundary(value, "interpolated", (left_x, right_x))

    components = []
    lower = Boundary(None, "scan_boundary", (x[0], x[0])) if y[0] <= threshold else None
    for index in range(len(x) - 1):
        if y[index] > threshold and y[index + 1] <= threshold:
            lower = crossing(index)
        if y[index] <= threshold and y[index + 1] > threshold:
            components.append(Interval(lower, crossing(index)))
            lower = None
    if lower is not None:
        components.append(Interval(lower, Boundary(None, "scan_boundary", (x[-1], x[-1]))))
    status = "no_sublevel_component" if not components else (
        "scan_boundary_open" if any(not component.bounded for component in components) else "resolved_on_grid")
    return IntervalResult(threshold, tuple(components), (x[0], x[-1]), minima, status)


def legacy_interval_row(scan, intervals):
    """Lossy historical list adapter; scientific code must use IntervalResult."""
    if not intervals.components:
        raise ValueError("legacy interval row cannot represent {}".format(intervals.status))
    if any(not component.bounded for component in intervals.components):
        warnings.warn("legacy +/-999 endpoints encode scan-boundary censoring, not physical limits; use structured=True", RuntimeWarning)
    if len(scan.minima) > 1 or len(intervals.components) > 2:
        warnings.warn("legacy row uses the leftmost tied minimum; all components retained; use structured=True", RuntimeWarning)
    row = [scan.wc, scan.minima[0],
           [component.lower.value if component.lower.value is not None else -999 for component in intervals.components],
           [component.upper.value if component.upper.value is not None else 999 for component in intervals.components]]
    if len(intervals.components) == 2:
        row.append(list(intervals.local_minima))
    return row


def nesting_differences(profiled, frozen, *, profiled_reference_nll, frozen_reference_nll, tolerance=0):
    """Return 2*(profiled NLL - frozen NLL) on exactly matching grids.

    Reference offsets must be supplied in a proven common NLL convention.
    Independently normalized display curves are never used here.
    """
    p_ref = finite(profiled_reference_nll, "profiled reference NLL")
    f_ref = finite(frozen_reference_nll, "frozen reference NLL")
    tolerance = finite(tolerance, "tolerance")
    if tolerance < 0:
        raise ValueError("tolerance must be nonnegative")
    if profiled.wc != frozen.wc or tuple(p.coordinate for p in profiled.grid_points) != tuple(p.coordinate for p in frozen.grid_points):
        raise ValueError("nesting diagnostic requires the same WC and exactly matching grids")
    differences = tuple((p.coordinate, finite(2 * ((p.delta_nll + p_ref) - (f.delta_nll + f_ref)), "NLL difference"))
                        for p, f in zip(profiled.grid_points, frozen.grid_points))
    return {"differences": differences, "violations": tuple(item for item in differences if item[1] > tolerance),
            "tolerance": tolerance, "quantity": "2*(profiled_NLL-frozen_NLL)"}


def interval_width_ratio(profiled, frozen):
    """Frozen width / profiled width; undefined for open/disconnected/zero width."""
    if profiled.threshold != frozen.threshold:
        raise ValueError("width ratio requires equal thresholds")
    if any(len(result.components) != 1 or not result.components[0].bounded for result in (profiled, frozen)):
        return None
    p = profiled.components[0]
    f = frozen.components[0]
    width = p.upper.value - p.lower.value
    return (f.upper.value - f.lower.value) / width if width > 0 else None

"""Validated MultiDimFit grid results and piecewise-linear reference intervals.

No analysis constants, ROOT runtime, plotting state, or fit execution lives here.
The input contract is one observed/Asimov grid result, not a mixture of toys.
"""

from dataclasses import dataclass, replace
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


VALID_GRID_POINT = "VALID_GRID_POINT"
INVALID_EVALUATION_POINT = "INVALID_EVALUATION_POINT"
SPECIAL_FIT_POINT = "SPECIAL_FIT_POINT"
FAILED_POINT_POLICIES = ("mask", "drop", "error")


@dataclass(frozen=True)
class ScanEntry:
    coordinate: float
    delta_nll: float
    kind: str
    tracked_parameters: tuple = ()
    locator: str = ""
    quantile_expected: object = None
    source_entry_index: object = None
    status: str = VALID_GRID_POINT
    reason_code: str = ""


@dataclass(frozen=True)
class ScanPoint:
    coordinate: float
    delta_nll: object
    twice_delta_nll: object
    entries: tuple
    status: str = VALID_GRID_POINT


@dataclass(frozen=True)
class ScanResult:
    wc: str
    raw_entries: tuple
    grid_entries: tuple
    special_entries: tuple
    points: tuple
    grid_points: tuple
    normalization_delta_nll: object
    duplicate_coordinates: tuple
    provenance: tuple
    classification: str
    failed_point_policy: str = "mask"
    family: str = "unspecified"

    @property
    def invalid_entries(self):
        return tuple(entry for entry in self.raw_entries if entry.status == INVALID_EVALUATION_POINT)

    @property
    def valid_points(self):
        return tuple(point for point in self.points if point.status != INVALID_EVALUATION_POINT)

    @property
    def invalid_coordinates(self):
        """Unrepaired finite coordinates; a valid duplicate supplies the value."""
        valid = {point.coordinate for point in self.valid_points}
        return tuple(sorted({entry.coordinate for entry in self.invalid_entries
                             if math.isfinite(entry.coordinate) and entry.coordinate not in valid}))

    @property
    def unknown_invalid_coordinate(self):
        return any(not math.isfinite(entry.coordinate) for entry in self.invalid_entries)

    @property
    def failed_points(self):
        """Recovery evidence only; no attempt selection or automatic retry."""
        return tuple({"wc": self.wc,
                      "coordinate": entry.coordinate if math.isfinite(entry.coordinate) else None,
                      "raw_coordinate": str(entry.coordinate),
                      "raw_producer_value": entry.delta_nll if math.isfinite(entry.delta_nll) else str(entry.delta_nll),
                      "source_locator": entry.locator, "source_entry_index": entry.source_entry_index,
                      "quantile_expected": entry.quantile_expected,
                      "tracked_parameters": {key: value if math.isfinite(value) else str(value)
                                             for key, value in entry.tracked_parameters},
                      "failure_reason": entry.reason_code, "family": self.family,
                      "coordinate_has_valid_candidate": math.isfinite(entry.coordinate)
                          and entry.coordinate in {point.coordinate for point in self.valid_points}}
                     for entry in self.invalid_entries)

    @property
    def sampled_grid_minima(self):
        if not self.grid_points:
            return ()
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
        return tuple(point.coordinate for point in self.valid_points if point.twice_delta_nll == 0)

    @property
    def curve_points(self):
        """Physical grid knots only; use curve_segments for continuity."""
        coordinates = {entry.coordinate for entry in self.raw_entries
                       if entry.kind != "special_fit" and math.isfinite(entry.coordinate)}
        return tuple(point for point in self.valid_points if point.coordinate in coordinates)

    @property
    def curve_segments(self):
        segments = []
        for point in self.curve_points:
            if (not segments or self.unknown_invalid_coordinate or
                    any(segments[-1][-1].coordinate < gap < point.coordinate
                        for gap in self.invalid_coordinates)):
                segments.append([])
            segments[-1].append(point)
        return tuple(tuple(segment) for segment in segments)

    @property
    def topology_complete(self):
        return not self.invalid_coordinates and not self.unknown_invalid_coordinate

    @property
    def minimum_status(self):
        # This is support for the supplied reference minimum, never proof of a
        # continuous/global minimum at unsampled or invalid coordinates.
        if not self.valid_points or self.unknown_invalid_coordinate:
            return "MINIMUM_UNRESOLVED"
        if any(entry.kind == "special_fit" and entry.coordinate in self.invalid_coordinates
               for entry in self.invalid_entries):
            return "MINIMUM_UNRESOLVED"
        if self.topology_complete:
            return "RESOLVED_ON_VALID_DATA"
        for minimum in self.minima:
            if not any(any(point.coordinate < minimum and point.twice_delta_nll > 0 for point in segment)
                       and any(point.coordinate > minimum and point.twice_delta_nll > 0 for point in segment)
                       for segment in self.curve_segments):
                return "MINIMUM_UNRESOLVED"
        return "RESOLVED_ON_VALID_SEGMENT"

    def intervals(self, threshold):
        threshold = finite(threshold, "threshold")
        if threshold < 0:
            raise ValueError("threshold must be nonnegative")
        if not self.curve_points:
            return IntervalResult(threshold, (), (), (), "MINIMUM_UNRESOLVED",
                                  topology_status="TOPOLOGY_INCOMPLETE", minimum_status=self.minimum_status,
                                  inference_status="MINIMUM_UNRESOLVED")
        results = []
        for segment in self.curve_segments:
            result = extract_intervals([point.coordinate for point in segment],
                                       [point.twice_delta_nll for point in segment], threshold)
            components = []
            # An isolated sublevel knot supports neither crossing, but retains
            # explicit open boundaries rather than silently losing the evidence.
            source_components = result.components
            if not self.topology_complete and len(segment) == 1 and segment[0].twice_delta_nll <= threshold:
                edge = Boundary(None, "insufficient_resolution", (segment[0].coordinate,) * 2)
                source_components = (Interval(edge, edge),)
            for component in source_components:
                boundaries = []
                for boundary, side in ((component.lower, "lower"), (component.upper, "upper")):
                    edge = segment[0 if side == "lower" else -1].coordinate
                    gaps = tuple(gap for gap in self.invalid_coordinates
                                 if (gap < edge if side == "lower" else gap > edge))
                    if boundary.value is None and (gaps or self.unknown_invalid_coordinate):
                        gap = (max(gaps) if side == "lower" else min(gaps)) if gaps else None
                        boundary = Boundary(None, "UNRESOLVED_DUE_TO_INVALID_POINT", (edge, gap))
                    boundaries.append(boundary)
                components.append(Interval(*boundaries))
            results.append(replace(result, components=tuple(components)))
        if self.topology_complete:
            result = results[0]
            central = tuple(component for component in result.components if component.bounded and
                            any(component.lower.value <= value <= component.upper.value for value in self.minima))
            resolved = (self.minimum_status != "MINIMUM_UNRESOLVED" and bool(central)
                        and all(any(component.lower.value <= value <= component.upper.value
                                    for component in central) for value in self.minima))
            return replace(result, minimum_status=self.minimum_status,
                           central_status="RESOLVED_ON_VALID_SEGMENT" if resolved else "INTERVAL_UNRESOLVED",
                           central_components=central if resolved else (),
                           inference_status="MINIMUM_UNRESOLVED" if self.minimum_status == "MINIMUM_UNRESOLVED" else
                               "VALID_COMPLETE" if result.status == "resolved_on_grid" else "INTERVAL_UNRESOLVED")
        components = tuple(component for result in results for component in result.components)
        central = tuple(component for component in components if component.bounded and
                        any(component.lower.value <= value <= component.upper.value for value in self.minima))
        blocked = any(boundary.kind == "UNRESOLVED_DUE_TO_INVALID_POINT"
                      for component in components for boundary in (component.lower, component.upper))
        minimum_status = self.minimum_status
        usable = (bool(central) and minimum_status != "MINIMUM_UNRESOLVED"
                  and all(any(component.lower.value <= value <= component.upper.value for component in central)
                          for value in self.minima))
        central_status = "RESOLVED_ON_VALID_SEGMENT" if usable else "INTERVAL_UNRESOLVED"
        if minimum_status == "MINIMUM_UNRESOLVED":
            inference_status = "MINIMUM_UNRESOLVED"
        elif usable and not blocked:
            inference_status = "USABLE_WITH_INVALID_TAIL_POINTS"
        elif usable or any(component.bounded for component in components):
            inference_status = "PARTIALLY_RESOLVED"
        else:
            inference_status = "INTERVAL_UNRESOLVED"
        status = "UNRESOLVED_DUE_TO_INVALID_POINT" if blocked else "TOPOLOGY_INCOMPLETE"
        return IntervalResult(threshold, components,
                              (self.curve_points[0].coordinate, self.curve_points[-1].coordinate),
                              tuple(value for result in results for value in result.local_minima), status,
                              topology_status="TOPOLOGY_INCOMPLETE", minimum_status=minimum_status,
                              central_status=central_status, central_components=central if usable else (),
                              inference_status=inference_status)


def canonicalize_entries(wc, entries, provenance=(), classification="explicit", *,
                         failed_point_policy="mask", family="unspecified"):
    if failed_point_policy not in FAILED_POINT_POLICIES:
        raise ValueError("failed_point_policy must be mask, drop, or error")
    if not isinstance(wc, str) or not wc:
        raise ValueError("scan WC name is required")
    entries = tuple(entries)
    if not entries:
        raise ValueError("empty scan result")
    checked = []
    for index, entry in enumerate(entries):
        if entry.kind not in {"grid", "special_fit", "unclassified"}:
            raise ValueError("unsupported entry kind: {}".format(entry.kind))
        coordinate, value = float(entry.coordinate), float(entry.delta_nll)
        reason = ("COMBINE_NLL_EVALUATION_ERROR_SENTINEL" if value == 9990 else
                  "NONFINITE_REQUIRED_COORDINATE" if not math.isfinite(coordinate) else
                  "NONFINITE_REQUIRED_LIKELIHOOD" if not math.isfinite(value) else "")
        status = INVALID_EVALUATION_POINT if reason else (
            SPECIAL_FIT_POINT if entry.kind == "special_fit" else VALID_GRID_POINT)
        tracked = tuple((key, float(value) if reason else finite(value, key))
                        for key, value in entry.tracked_parameters)
        checked.append(replace(entry, coordinate=coordinate, delta_nll=value, status=status,
                               reason_code=reason, tracked_parameters=tracked,
                               source_entry_index=entry.source_entry_index if entry.source_entry_index is not None else index))
    entries = tuple(checked)
    if failed_point_policy == "error" and any(entry.status == INVALID_EVALUATION_POINT for entry in entries):
        raise ValueError("invalid evaluation point under failed_point_policy=error")
    valid = tuple(entry for entry in entries if entry.status != INVALID_EVALUATION_POINT)
    grid = tuple(entry for entry in valid if entry.kind != "special_fit")
    special = tuple(entry for entry in valid if entry.kind == "special_fit")
    if not any(entry.kind != "special_fit" for entry in entries):
        raise ValueError("result contains no grid entries")
    baseline = min((entry.delta_nll for entry in valid), default=None)

    def points_for(selected):
        grouped = {}
        for entry in selected:
            grouped.setdefault(entry.coordinate, []).append(entry)
        return tuple(ScanPoint(
            coordinate, min(entry.delta_nll for entry in group),
            finite(2 * (min(entry.delta_nll for entry in group) - baseline), "normalized 2*deltaNLL"),
            tuple(sorted(group, key=lambda entry: (entry.delta_nll, entry.kind, entry.tracked_parameters, entry.locator))),
            VALID_GRID_POINT if any(entry.kind != "special_fit" for entry in group) else SPECIAL_FIT_POINT,
        ) for coordinate, group in sorted(grouped.items()))

    points = points_for(valid)
    valid_coordinates = {point.coordinate for point in points}
    if failed_point_policy == "mask":
        masked = {}
        for entry in entries:
            if entry.status == INVALID_EVALUATION_POINT and entry.coordinate not in valid_coordinates:
                masked.setdefault(entry.coordinate, []).append(entry)
        points += tuple(ScanPoint(coordinate, None, None, tuple(group), INVALID_EVALUATION_POINT)
                        for coordinate, group in masked.items())
        points = tuple(sorted(points, key=lambda point: (not math.isfinite(point.coordinate),
                                                        point.coordinate if math.isfinite(point.coordinate) else 0)))
    counts = {}
    for entry in entries:
        if math.isfinite(entry.coordinate):
            counts[entry.coordinate] = counts.get(entry.coordinate, 0) + 1
    return ScanResult(wc, entries, grid, special, points, points_for(grid), baseline,
                      tuple((coordinate, count) for coordinate, count in sorted(counts.items()) if count > 1),
                      tuple(provenance), classification, failed_point_policy, family)


def scan_from_columns(columns, wc, locator="memory", *, failed_point_policy="mask", family="unspecified"):
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
                                 tuple((key, columns[key][index]) for key in tracked), entry_locator, quantile, index))
    return canonicalize_entries(wc, entries, (str(locator),), classification,
                                failed_point_policy=failed_point_policy, family=family)


def read_scan(path, wc, *, failed_point_policy="mask", family="unspecified"):
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
            return scan_from_columns(columns, wc, str(path), failed_point_policy=failed_point_policy, family=family)
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
    if len({(scan.failed_point_policy, scan.family) for scan in scans}) != 1:
        raise ValueError("input shards must use the same failed_point_policy and family")
    if len(scans) == 1:
        return scans[0]
    return canonicalize_entries(scans[0].wc,
                               (entry for scan in scans for entry in scan.raw_entries),
                               (path for scan in scans for path in scan.provenance),
                               "combined_common_reference", failed_point_policy=scans[0].failed_point_policy, family=scans[0].family)


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
    topology_status: str = "COMPLETE_ON_SUPPLIED_GRID"
    minimum_status: str = "RESOLVED_ON_VALID_DATA"
    central_status: str = "NOT_SEPARATELY_EVALUATED"
    central_components: tuple = ()
    inference_status: str = "VALID_COMPLETE"


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
        return IntervalResult(threshold, (), (x[0], x[-1]), minima, "insufficient_resolution",
                              inference_status="INTERVAL_UNRESOLVED")

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
    return IntervalResult(threshold, tuple(components), (x[0], x[-1]), minima, status,
                          inference_status="VALID_COMPLETE" if status == "resolved_on_grid" else "INTERVAL_UNRESOLVED")


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
    def coordinates(scan):
        return {entry.coordinate for entry in scan.raw_entries
                if entry.kind != "special_fit" and math.isfinite(entry.coordinate)}

    unknown_coordinate = profiled.unknown_invalid_coordinate or frozen.unknown_invalid_coordinate
    if profiled.wc != frozen.wc or (coordinates(profiled) != coordinates(frozen) and not unknown_coordinate):
        raise ValueError("nesting diagnostic requires the same WC and exactly matching grids")
    p_points = {point.coordinate: point for point in profiled.grid_points}
    f_points = {point.coordinate: point for point in frozen.grid_points}
    shared = sorted(p_points.keys() & f_points.keys())
    differences = tuple((coordinate, finite(2 * ((p_points[coordinate].delta_nll + p_ref)
                          - (f_points[coordinate].delta_nll + f_ref)), "NLL difference")) for coordinate in shared)
    invalid_coordinates = set(profiled.invalid_coordinates) | set(frozen.invalid_coordinates)
    unavailable = tuple({"coordinate": coordinate,
                         "reason": "INVALID_EVALUATION_POINT" if coordinate in invalid_coordinates else "NO_VALID_COUNTERPART"}
                        for coordinate in sorted((coordinates(profiled) | coordinates(frozen)) - set(shared)))
    unavailable += tuple({"coordinate": None, "reason": "NONFINITE_REQUIRED_COORDINATE",
                          "family": scan.family, "source_locator": entry.locator}
                         for scan in (profiled, frozen) for entry in scan.invalid_entries
                         if not math.isfinite(entry.coordinate))
    unresolved = not shared or any(scan.minimum_status == "MINIMUM_UNRESOLVED" for scan in (profiled, frozen))
    return {"differences": differences, "violations": tuple(item for item in differences if item[1] > tolerance),
            "unavailable": unavailable,
            "status": "UNRESOLVED" if unresolved else "PARTIALLY_RESOLVED" if unavailable else "VALID_COMPLETE",
            "tolerance": tolerance, "quantity": "2*(profiled_NLL-frozen_NLL)"}


def interval_width_ratio(profiled, frozen):
    """Frozen width / profiled width; undefined for open/disconnected/zero width."""
    if profiled.threshold != frozen.threshold:
        raise ValueError("width ratio requires equal thresholds")
    if any(result.topology_status != "COMPLETE_ON_SUPPLIED_GRID" or
           result.minimum_status == "MINIMUM_UNRESOLVED" for result in (profiled, frozen)):
        return None
    if any(len(result.components) != 1 or not result.components[0].bounded for result in (profiled, frozen)):
        return None
    p = profiled.components[0]
    f = frozen.components[0]
    width = p.upper.value - p.lower.value
    return (f.upper.value - f.lower.value) / width if width > 0 else None

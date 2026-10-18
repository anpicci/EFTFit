"""Synthetic consumer contracts; no workspace, Combine, or scheduler execution."""

import ast
from dataclasses import asdict
import importlib.util
from pathlib import Path
import sys
import types

import numpy as np
import pytest
import uproot
import yaml


fitter_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(fitter_dir / "python"))
from scan_1d import (ScanEntry, canonicalize_entries, scan_from_columns, read_scan,
                     combine_scans, extract_intervals,
                     nesting_differences, interval_width_ratio)


def make_scan(x, y, special=()):
    return canonicalize_entries("c", [ScanEntry(a, b / 2, "grid") for a, b in zip(x, y)] + list(special))


def bounds(result):
    return [(component.lower.value, component.upper.value) for component in result.components]


def test_smooth_connected_interval():
    result = make_scan([-2, -1, 0, 1, 2], [4, 1, 0, 1, 4]).intervals(2)
    assert bounds(result)[0] == pytest.approx((-4 / 3, 4 / 3))
    assert result.components[0].lower.kind == "interpolated"
    assert result.components[0].lower.bracket == (-2, -1)


def test_two_disconnected_components_and_two_local_minima():
    scan = make_scan([-3, -2, -1, 0, 1, 2, 3], [4, 0, 4, 5, 4, 0, 4])
    result = scan.intervals(1)
    assert bounds(result) == [(-2.25, -1.75), (1.75, 2.25)]
    assert result.local_minima == (-2, 2)
    assert scan.sampled_grid_minima == (-2, 2)



@pytest.mark.parametrize("y, expected", [([0, 0, 2], [(None, 1.5)]), ([2, 0, 0], [(0.5, None)]), ([0, 0, 0], [(None, None)])])
def test_open_intervals_and_no_crossing(y, expected):
    result = extract_intervals([0, 1, 2], y, 1)
    assert bounds(result) == expected
    assert result.status == "scan_boundary_open"


def test_missing_crossings_and_insufficient_resolution():
    assert extract_intervals([0, 1], [2, 3], 1).status == "no_sublevel_component"
    assert extract_intervals([0], [0], 1).status == "insufficient_resolution"



def test_exact_threshold_tangent_and_plateau():
    assert bounds(extract_intervals([0, 1, 2], [2, 1, 2], 1)) == [(1, 1)]
    assert bounds(extract_intervals([0, 1, 2, 3], [2, 1, 1, 2], 1)) == [(1, 2)]
    assert bounds(extract_intervals([0, 1, 2], [0, 1, 0], 1)) == [(None, None)]


def test_duplicate_minimum_is_deterministic_and_retains_special_entry():
    entries = [ScanEntry(0, 4, "grid"), ScanEntry(0, 2, "grid"),
               ScanEntry(0, 0, "special_fit"), ScanEntry(1, 3, "grid")]
    first = canonicalize_entries("c", entries)
    second = canonicalize_entries("c", reversed(entries))
    assert [(p.coordinate, p.delta_nll) for p in first.points] == [(p.coordinate, p.delta_nll) for p in second.points]
    assert first.points[0].delta_nll == 0
    assert first.grid_points[0].delta_nll == 2
    assert first.curve_points[0].delta_nll == 0
    assert first.duplicate_coordinates == ((0, 3),)
    assert len(first.grid_entries) == 3
    assert first.reported_fit_minima == (0,)


def test_unsorted_and_grid_minimum_below_special_fit():
    scan = make_scan([1, -1, 0], [4, 4, -2], (ScanEntry(0.1, 0, "special_fit"),))
    assert [point.coordinate for point in scan.grid_points] == [-1, 0, 1]
    assert scan.normalization_delta_nll == -1
    assert scan.sampled_grid_minima == (0,)
    assert scan.reported_fit_minima == (0.1,)
    assert scan.minima == (0,)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_classification_and_array_rejection(value):
    for x, y in (([0, value], [0, 1]), ([0, 1], [0, value])):
        assert len(make_scan(x, y).invalid_entries) == 1
        with pytest.raises(ValueError, match="finite"):
            extract_intervals(x, y, 1)


def test_invalid_shape_order_and_empty_result():
    for x, y in (([], []), ([0, 0], [0, 1]), ([1, 0], [0, 1]), ([0], [0, 1])):
        with pytest.raises(ValueError):
            extract_intervals(x, y, 1)
    with pytest.raises(ValueError, match="empty"):
        make_scan([], [])


def write_root(path, columns=None, tree="limit"):
    if columns is None:
        columns = {"c": [-2, -1, 0, 1, 2], "deltaNLL": [2, 0.5, 0, 0.5, 2], "quantileExpected": [0] * 5}
    with uproot.recreate(path) as output:
        output.mktree(tree, {name: "float64" for name in columns})
        output[tree].extend({name: np.asarray(values, dtype=float) for name, values in columns.items()})
    return path


@pytest.mark.parametrize("columns, tree, message", [
    ({"c": [0], "deltaNLL": [0]}, "other", "missing limit"),
    ({"deltaNLL": [0]}, "limit", "branch c"),
    ({"c": [0]}, "limit", "branch deltaNLL"),
    ({"c": [], "deltaNLL": []}, "limit", "empty"),
])
def test_missing_root_tree_branches_and_malformed_rows(tmp_path, columns, tree, message):
    path = write_root(tmp_path / "bad.root", columns, tree)
    with pytest.raises(ValueError, match=message):
        read_scan(path, "c")


def test_root_roundtrip_tracked_values_and_order_independent_classification(tmp_path):
    path = write_root(tmp_path / "scan.root", {
        "c": [1, 0, -1, 0], "deltaNLL": [2, 0, 2, 0.1],
        "quantileExpected": [0, -1, 0, 0], "trackedParam_other": [3, 4, 5, 6], "iToy": [-1] * 4})
    scan = read_scan(path, "c")
    assert len(scan.grid_entries) == 3
    assert len(scan.special_entries) == 1
    assert scan.special_entries[0].tracked_parameters == (("trackedParam_other", 4),)
    assert str(path) in scan.special_entries[0].locator


def test_missing_classification_and_multiple_toys():
    with pytest.warns(RuntimeWarning, match="unclassified"):
        scan = scan_from_columns({"c": [0, 1], "deltaNLL": [0, 1]}, "c")
    assert scan.classification == "unclassified_as_grid"
    assert scan.reported_fit_minima == ()
    with pytest.raises(ValueError, match="mixed toys"):
        scan_from_columns({"c": [0, 1], "deltaNLL": [0, 1], "iToy": [1, 2]}, "c")


def test_different_minima_require_raw_reference_offsets():
    profiled = make_scan([-1, 0, 1], [0, 2, 8])
    frozen = make_scan([-1, 0, 1], [8, 2, 0])
    with pytest.raises(TypeError):
        nesting_differences(profiled, frozen)
    result = nesting_differences(profiled, frozen, profiled_reference_nll=10, frozen_reference_nll=14)
    assert result["differences"] == ((-1, -16), (0, -8), (1, 0))
    assert result["violations"] == ()
    shifted_profiled = make_scan([-1, 0, 1], [20, 22, 28])
    shifted_frozen = make_scan([-1, 0, 1], [48, 42, 40])
    result = nesting_differences(shifted_profiled, shifted_frozen, profiled_reference_nll=0, frozen_reference_nll=0)
    assert result["differences"] == ((-1, -28), (0, -20), (1, -12))


def test_nested_curves_and_deliberate_violation():
    profiled = make_scan([-1, 0, 1], [1, 0, 1])
    frozen = make_scan([-1, 0, 1], [2, 0, 2])
    assert not nesting_differences(profiled, frozen, profiled_reference_nll=0, frozen_reference_nll=0)["violations"]
    assert nesting_differences(frozen, profiled, profiled_reference_nll=0, frozen_reference_nll=0)["violations"] == ((-1, 1), (1, 1))
    with pytest.raises(ValueError, match="matching grids"):
        nesting_differences(profiled, make_scan([0, 1], [0, 1]), profiled_reference_nll=0, frozen_reference_nll=0)


def test_multiple_files_require_explicit_reference_contract():
    scan = make_scan([-1, 0, 1], [2, 0, 2])
    with pytest.raises(ValueError, match="common_reference"):
        combine_scans([scan, scan])
    with pytest.raises(ValueError, match="common_reference"):
        combine_scans([scan, scan], common_reference="false")
    assert combine_scans([scan, scan], common_reference=True).duplicate_coordinates == ((-1, 2), (0, 2), (1, 2))


def test_more_than_two_components_and_open_structured_boundaries():
    scan = make_scan(range(7), [0, 2, 0, 2, 0, 2, 0])
    result = scan.intervals(1)
    assert len(result.components) == 4
    assert result.components[0].lower.value is None
    assert result.components[-1].upper.value is None


def test_width_ratio_has_defined_domain():
    profiled = extract_intervals([-2, 0, 2], [2, 0, 2], 1)
    frozen = extract_intervals([-1, 0, 1], [2, 0, 2], 1)
    assert interval_width_ratio(profiled, frozen) == 0.5
    assert interval_width_ratio(profiled, extract_intervals([0, 1], [0, 0], 1)) is None
    assert interval_width_ratio(profiled, extract_intervals(range(5), [2, 0, 2, 0, 2], 1)) is None


@pytest.fixture
def plot_module(monkeypatch):
    # Expose the CMSSW package layout without setting up or modifying CMSSW.
    for name, directory in (("EFTFit", fitter_dir.parent), ("EFTFit.Fitter", fitter_dir / "python")):
        package = types.ModuleType(name)
        package.__path__ = [str(directory)]
        monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(sys.modules, "EFTFit.Fitter.scan_1d", sys.modules["scan_1d"])
    spec = importlib.util.spec_from_file_location("consumer_plotter", fitter_dir / "scripts" / "EFTPlotter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configured_plotter(module, tmp_path, **kwargs):
    config = tmp_path / "fit.yml"
    config.write_text(yaml.safe_dump({"analysis": "test", "wc_population": {"scientific": ["c"], "fixed_zero": ["fixed"]}, "wc_ranges": {"c": [-12, 12]}}))
    return module.EFTPlot(fit_config=config,
                          display_config={"output_directory": str(tmp_path), "formats": ["png"], "scales": {"c": 5}},
                          result_options={"dir_path": str(tmp_path), "postfix": ".mH120"}, **kwargs)


def test_metadata_authority_and_legacy_no_argument_mode(plot_module, tmp_path):
    plotter = configured_plotter(plot_module, tmp_path)
    assert plotter.wc_ranges == {"c": (-12, 12)}
    assert plotter.wcs == ["c"]
    assert plotter.display.luminosity == plotter.display.collision_energy == ""
    with pytest.raises(ValueError, match="conflicts"):
        configured_plotter(plot_module, tmp_path, wc_ranges={"c": [-1, 1]})
    with pytest.warns(RuntimeWarning, match="non-authoritative"):
        legacy = plot_module.EFTPlot()
    assert legacy.wc_ranges == {}
    with pytest.raises(ValueError, match="explicit WCs"):
        legacy.getIntervalFits()
    with pytest.raises(ValueError, match="historical fit numbers are retired"):
        plotter.BestFitPlot()


def test_compatibility_arrays_and_interval_siginterval(plot_module, tmp_path):
    plotter = configured_plotter(plot_module, tmp_path)
    write_root(tmp_path / "higgsCombine.p.c.MultiDimFit.mH120.root")
    x, y = plotter.GetWCsNLLFromRoot([".p.c"], "c", unique=True)
    assert x == [-2, -1, 0, 1, 2] and y == [4, 1, 0, 1, 4]
    assert bounds(plotter.getIntervalFits(basename_lst=[".p"], siginterval=1)[0]["intervals"]) == [(-1, 1)]
    summaries = plotter.getIntervalFits(basename_lst=[".p"], siginterval=2)
    assert summaries[0]["intervals"].threshold == 4
    assert summaries[0]["intervals"].semantics == "asymptotic_reference"


def test_compatibility_arrays_do_not_rezero_raw_values(plot_module, tmp_path, monkeypatch):
    plotter = configured_plotter(plot_module, tmp_path)
    scan = make_scan([0, 1], [10, 12])
    monkeypatch.setattr(plotter, "read_scan_result", lambda *args, **kwargs: scan)
    assert plotter.GetWCsNLLFromRoot([".p"], "c", unique=True) == [[0, 1], [10, 12]]
    assert [point.twice_delta_nll for point in scan.curve_points] == [0, 2]


def test_distinct_input_lists_validation_and_union_extent(plot_module, tmp_path):
    plotter = configured_plotter(plot_module, tmp_path)
    write_root(tmp_path / "higgsCombine.p.c.MultiDimFit.mH120.root")
    with pytest.raises(ValueError, match="higgsCombine.f.c"):
        plotter.OverlayLLPlot1DEFT(name1_lst=[".p.c"], name2_lst=[".f.c"], wc="c")
    write_root(tmp_path / "higgsCombine.f.c.MultiDimFit.mH120.root", {"c": [-4, 0, 4], "deltaNLL": [3, 0, 3], "quantileExpected": [0] * 3})
    result = plotter.OverlayLLPlot1DEFT(name1_lst=[".p.c"], name2_lst=[".f.c"], wc="c")
    assert result["display_extent"] == (-20, 20)
    assert Path(result["paths"][0]).stat().st_size > 0
    assert result["scans"][1].grid_points[-1].coordinate == 4


def test_scaling_and_summary_do_not_mutate_physical_intervals(plot_module, tmp_path, monkeypatch):
    plotter = configured_plotter(plot_module, tmp_path)
    scan = make_scan(range(-4, 5), [0, 2, 5, 2, 0, 2, 5, 2, 0])
    before = asdict(scan)
    monkeypatch.setattr(plotter, "read_scan_result", lambda *args, **kwargs: scan)
    result = plotter.BestScanPlot([".p"], [".f"], wcs=["c"], printFOM=True)
    assert asdict(scan) == before
    assert result["width_ratios"] == {"c": {1: None, 4: None}}
    assert Path(result["paths"][0]).stat().st_size > 0
    assert plotter.display.label("c") == "c ×5"


def test_single_and_batch_public_adapters(plot_module, tmp_path):
    plotter = configured_plotter(plot_module, tmp_path)
    for family in ("p", "f"):
        write_root(tmp_path / ("higgsCombine.{}.c.MultiDimFit.mH120.root".format(family)))
    assert plotter.LLPlot1DEFT([".p.c"], wc="c")["scans"][0].wc == "c"
    assert len(plotter.BatchOverlayLLPlot1DEFT([".p"], [".f"], wcs=["c"])) == 1
    assert plotter.BestScanPlot([".p"], [".f"], wcs=["c"])["rows"][0][0] == "c"


def test_canonical_resolver_remains_in_use(plot_module, tmp_path, monkeypatch):
    plotter = configured_plotter(plot_module, tmp_path)
    path = write_root(tmp_path / "canonical.root")
    calls = []
    def resolve(name, identity, directory):
        calls.append((name, identity, directory))
        return path
    monkeypatch.setattr(plot_module, "resolve_canonical_aggregate", resolve)
    plotter.GetWCsNLLFromRoot([".p.c"], "c", postfix="", job_set_ids={".p.c": "job"})
    assert calls == [(".p.c", "job", str(tmp_path))]


def test_package_constructor_and_explicit_display_range(plot_module, tmp_path):
    configured_plotter(plot_module, tmp_path)
    package = tmp_path / "package"
    (package / "config").mkdir(parents=True)
    (package / "datacards").mkdir()
    (package / "config" / "TOP_26_006.yml").write_text((tmp_path / "fit.yml").read_text())
    plotter = plot_module.EFTPlot(fit_package_root=package, display_config={
        "ranges": {"c": [-3, 4]}, "formats": ["png"], "output_directory": str(tmp_path)})
    assert plotter.wc_ranges == {"c": (-12, 12)}
    scan = make_scan([-2, 0, 2], [2, 0, 2])
    before = asdict(scan)
    result = plot_module.render_curves([scan], ["curve"], plotter.display, "explicit_extent")
    assert result["display_extent"] == (-3, 4)
    assert asdict(scan) == before


def test_parse_nll_helpers_use_core_and_validate_branches(plot_module, tmp_path):
    spec = importlib.util.spec_from_file_location("consumer_parse_nll", fitter_dir / "scripts" / "parse_nll.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.get_unique_points({"x": [1, 0, 1], "nll": [4, 0, 2], "other": [10, 20, 30]}, "x", "nll")
    assert result == {"x": [0, 1], "nll": [0, 2], "other": [20, 30]}
    path = write_root(tmp_path / "helpers.root")
    assert module.get_vals_from_root_file(path, ["c", "deltaNLL"])["c"] == [-2, -1, 0, 1, 2]
    with pytest.raises(ValueError, match="missing required"):
        module.get_vals_from_root_file(path, ["missing"])
    with pytest.raises(ValueError, match="poi_lst"):
        module.get_best_points_by_wc([{"c": 0}])
    assert module.get_best_points_by_wc([{"c": 0}], ["c"]) == {"c": [0]}
    with pytest.raises(ValueError, match="invalid evaluation"):
        module.get_unique_points({"x": [0], "nll": [float("nan")]}, "x", "nll")


def test_overlay_rejects_malformed_second_family(plot_module, tmp_path):
    plotter = configured_plotter(plot_module, tmp_path)
    write_root(tmp_path / "higgsCombine.p.c.MultiDimFit.mH120.root")
    write_root(tmp_path / "higgsCombine.f.c.MultiDimFit.mH120.root", {"c": [0]})
    with pytest.raises(ValueError, match="branch deltaNLL"):
        plotter.OverlayLLPlot1DEFT(name1_lst=[".p.c"], name2_lst=[".f.c"], wc="c")
    assert not list(tmp_path.glob("Overlay*.png"))


def test_tracked_parameter_nonfinite_and_special_only_rejected():
    with pytest.raises(ValueError, match="trackedParam_c"):
        scan_from_columns({"c": [0], "deltaNLL": [0], "quantileExpected": [0], "trackedParam_c": [float("inf")]}, "c")
    with pytest.raises(ValueError, match="no grid"):
        scan_from_columns({"c": [0], "deltaNLL": [0], "quantileExpected": [-1]}, "c")
    with pytest.raises(ValueError, match="negative quantile"):
        scan_from_columns({"c": [0], "deltaNLL": [0], "quantileExpected": [-0.32]}, "c")


def test_retrieval_api_signatures_remain():
    tree = ast.parse((fitter_dir / "scripts" / "EFTFitter.py").read_text())
    methods = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert [arg.arg for arg in methods["batchRetrieve1DScansEFT"].args.args] == ["self", "basename", "batch", "scan_wcs", "job_set_ids"]
    assert [arg.arg for arg in methods["retrieveGridScan"].args.args] == ["self", "name", "batch", "user", "require_condor_provenance", "job_set_id", "execution_root"]

"""Hard-invalid producer rows, segment topology, and recovery contracts."""

import ast
from dataclasses import asdict
import importlib.util
import json
import math
from pathlib import Path

import pytest

from test_scan_1d_consumer import (plot_module, configured_plotter, write_root,
                                   fitter_dir, bounds)
from scan_1d import (ScanEntry, canonicalize_entries, scan_from_columns, read_scan,
                     combine_scans, nesting_differences, interval_width_ratio,
                     INVALID_EVALUATION_POINT, VALID_GRID_POINT, SPECIAL_FIT_POINT)


def invalid_scan(x=(-3, -2, -1, 0, 1, 2, 3), y=(9990, 2, .5, 0, .5, 2, 4), **kwargs):
    return scan_from_columns({"c": x, "deltaNLL": y, "quantileExpected": [0] * len(x),
                              "trackedParam_other": list(range(len(x)))}, "c", "member.root", **kwargs)


def test_sentinel_classification_provenance_and_recovery():
    scan = invalid_scan(family="profiled")
    row = scan.invalid_entries[0]
    assert row.status == INVALID_EVALUATION_POINT
    assert row.delta_nll == 9990 and row.coordinate == -3
    assert row.quantile_expected == 0 and row.source_entry_index == 0
    assert row.locator == "member.root:limit[0]"
    assert row.tracked_parameters == (("trackedParam_other", 0),)
    assert row.reason_code == "COMBINE_NLL_EVALUATION_ERROR_SENTINEL"
    assert scan.grid_entries[0].status == VALID_GRID_POINT
    recovery = json.loads(json.dumps(scan.failed_points, allow_nan=False))
    assert recovery[0]["wc"] == "c" and recovery[0]["family"] == "profiled"
    assert recovery[0]["source_locator"] == row.locator
    assert recovery[0]["failure_reason"] == row.reason_code


def test_invalid_never_normalizes_or_becomes_physical_even_below_valid_values():
    scan = invalid_scan([0, 1, 2], [10000, 9990, 10001])
    assert scan.normalization_delta_nll == 10000
    assert scan.minima == scan.sampled_grid_minima == (0,)
    assert [point.twice_delta_nll for point in scan.curve_points] == [0, 2]
    assert all(point.delta_nll != 9990 for point in scan.grid_points)
    assert scan.points[1].delta_nll is scan.points[1].twice_delta_nll is None


@pytest.mark.parametrize("policy, ordinary_size", [("mask", 5), ("drop", 4)])
def test_policies_preserve_provenance_and_segment_break(policy, ordinary_size):
    scan = invalid_scan([-2, -1, 0, 1, 2], [2, 0, 9990, 0, 2], failed_point_policy=policy)
    assert len(scan.points) == ordinary_size
    assert len(scan.invalid_entries) == 1 and len(scan.raw_entries) == 5
    assert scan.invalid_coordinates == (0,)
    assert [[point.coordinate for point in segment] for segment in scan.curve_segments] == [[-2, -1], [1, 2]]
    assert scan.intervals(1).status == "UNRESOLVED_DUE_TO_INVALID_POINT"
    assert bounds(scan.intervals(1)) == [(-1.25, None), (None, 1.25)]
    assert scan.intervals(1).components[0].upper.bracket == (-1, 0)
    assert scan.intervals(1).minimum_status == "MINIMUM_UNRESOLVED"


@pytest.mark.parametrize("policy", ["mask", "drop"])
def test_blocked_crossing_with_resolved_minimum(policy):
    scan = invalid_scan([-3, -2, -1, 0, 1, 2, 3], [3, 9990, .2, 0, .2, 2, 3], failed_point_policy=policy)
    result = scan.intervals(1)
    assert result.minimum_status == "RESOLVED_ON_VALID_SEGMENT"
    assert result.inference_status == "INTERVAL_UNRESOLVED"
    assert result.components[0].lower.kind == "UNRESOLVED_DUE_TO_INVALID_POINT"
    assert result.components[0].lower.value is None
    assert result.central_components == ()
    assert result.components[0].upper.value == pytest.approx(1 + .6 / 3.6)
    assert interval_width_ratio(result, result) is None


def test_remote_invalid_tail_leaves_central_interval_usable():
    scan = invalid_scan()
    result = scan.intervals(1)
    assert bounds(result) == [(-1, 1)]
    assert result.central_components == result.components
    assert result.central_status == "RESOLVED_ON_VALID_SEGMENT"
    assert result.inference_status == "USABLE_WITH_INVALID_TAIL_POINTS"
    assert result.topology_status == "TOPOLOGY_INCOMPLETE"
    assert scan.intervals(5).status == "UNRESOLVED_DUE_TO_INVALID_POINT"
    assert interval_width_ratio(result, result) is None  # Full-set width is unknown.


def test_partial_interval_retains_resolved_central_component():
    scan = invalid_scan([-4, -3, -2, -1, 0, 1, 2], [.1, 9990, 2, .5, 0, .5, 2])
    result = scan.intervals(1)
    assert result.inference_status == "PARTIALLY_RESOLVED"
    assert len(result.central_components) == 1
    assert result.central_components[0].bounded
    assert result.topology_status == "TOPOLOGY_INCOMPLETE"


def test_valid_duplicate_wins_and_invalid_duplicate_evidence_survives():
    scan = invalid_scan([-1, 0, 0, 0, 1], [2, 9990, .4, 0, 2])
    assert [point.delta_nll for point in scan.grid_points] == [2, 0, 2]
    assert len(scan.invalid_entries) == 1
    assert scan.duplicate_coordinates == ((0, 3),)
    assert scan.invalid_coordinates == () and len(scan.curve_segments) == 1
    assert scan.failed_points[0]["coordinate_has_valid_candidate"] is True
    assert scan.intervals(1).inference_status == "VALID_COMPLETE"
    with pytest.raises(ValueError, match="invalid evaluation"):
        invalid_scan([-1, 0, 0, 1], [2, 9990, 0, 2], failed_point_policy="error")


def test_all_invalid_duplicates_and_all_invalid_scan_have_no_manufactured_value():
    scan = invalid_scan([0, 0], [9990, 9990])
    assert len(scan.points) == 1 and len(scan.points[0].entries) == 2
    assert scan.points[0].status == INVALID_EVALUATION_POINT
    assert scan.normalization_delta_nll is None
    assert scan.grid_points == scan.minima == scan.sampled_grid_minima == ()
    assert scan.intervals(1).status == "MINIMUM_UNRESOLVED"
    assert len(scan.failed_points) == 2


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_required_values_are_hard_invalid_with_recovery(value):
    scan = invalid_scan([0, 1, 2], [0, value, 2])
    assert scan.invalid_entries[0].reason_code == "NONFINITE_REQUIRED_LIKELIHOOD"
    assert len(scan.curve_segments) == 2
    scan = invalid_scan([0, value, 2], [0, 1, 2])
    assert scan.unknown_invalid_coordinate and len(scan.curve_segments) == 2
    assert scan.minimum_status == "MINIMUM_UNRESOLVED"
    assert scan.failed_points[0]["coordinate"] is None
    json.dumps(scan.failed_points, allow_nan=False)


def test_special_fit_failure_does_not_set_reference():
    scan = scan_from_columns({"c": [-1, 0, 1, .1], "deltaNLL": [2, 1, 2, 9990],
                              "quantileExpected": [0, 0, 0, -1]}, "c")
    assert scan.normalization_delta_nll == 1
    assert scan.reported_fit_minima == ()
    assert scan.minimum_status == scan.intervals(1).minimum_status == "MINIMUM_UNRESOLVED"
    valid = scan_from_columns({"c": [-1, 0, 1], "deltaNLL": [2, 0, 2],
                               "quantileExpected": [0, -1, 0]}, "c")
    assert valid.special_entries[0].status == SPECIAL_FIT_POINT


@pytest.mark.parametrize("value", [9989, 9990.00001, 9999, 10000, 1e100])
def test_large_finite_non_sentinel_values_are_valid(value):
    scan = invalid_scan([0, 1], [0, value])
    assert scan.invalid_entries == ()
    assert scan.grid_points[1].twice_delta_nll == 2 * value


def test_strict_and_invalid_policy_and_shards():
    with pytest.raises(ValueError, match="invalid evaluation"):
        invalid_scan(failed_point_policy="error")
    with pytest.raises(ValueError, match="mask, drop, or error"):
        invalid_scan(failed_point_policy="remove")
    scan = invalid_scan(failed_point_policy="drop", family="frozen")
    combined = combine_scans([scan, scan], common_reference=True)
    assert combined.failed_point_policy == "drop" and combined.family == "frozen"
    assert len(combined.invalid_entries) == 2 and combined.invalid_coordinates == (-3,)
    with pytest.raises(ValueError, match="same failed_point_policy"):
        combine_scans([scan, invalid_scan()], common_reference=True)


def test_nesting_skips_bad_coordinate_but_compares_remaining_values():
    profiled = invalid_scan()
    frozen = invalid_scan(y=[5, 3, 1, 0, 1, 3, 5])
    result = nesting_differences(profiled, frozen, profiled_reference_nll=10, frozen_reference_nll=10)
    assert result["unavailable"] == ({"coordinate": -3, "reason": INVALID_EVALUATION_POINT},)
    assert result["differences"] == ((-2, -2), (-1, -1), (0, 0), (1, -1), (2, -2), (3, -2))
    assert result["status"] == "PARTIALLY_RESOLVED" and result["violations"] == ()
    affected = invalid_scan(y=[4, 2, .5, 9990, .5, 2, 4])
    result = nesting_differences(affected, frozen, profiled_reference_nll=10, frozen_reference_nll=10)
    assert result["status"] == "UNRESOLVED"
    assert len(result["differences"]) == 6


@pytest.mark.parametrize("policy", ["mask", "drop"])
def test_root_reader_and_public_consumer_propagate_policy(plot_module, tmp_path, policy):
    plotter = configured_plotter(plot_module, tmp_path)
    path = write_root(tmp_path / "higgsCombine.p.c.MultiDimFit.mH120.root", {
        "c": [-2, -1, 0, 1, 2], "deltaNLL": [2, 0, 9990, 0, 2], "quantileExpected": [0] * 5})
    scan = plotter.read_scan_result([".p.c"], "c", failed_point_policy=policy, family="profiled")
    assert scan.failed_point_policy == policy and len(scan.curve_segments) == 2
    assert scan.failed_points[0]["family"] == "profiled"
    with pytest.raises(ValueError, match="invalid evaluation"):
        read_scan(path, "c", failed_point_policy="error")
    with pytest.raises(ValueError, match="plain arrays"):
        plotter.GetWCsNLLFromRoot([".p.c"], "c", failed_point_policy=policy)
    result = plotter.getIntervalFits(basename_lst=[".p"], failed_point_policy=policy)[0]
    assert result["failed_points"] and result["intervals"].status == "UNRESOLVED_DUE_TO_INVALID_POINT"
    with pytest.raises(TypeError, match="unknown result"):
        plotter.read_scan_result([".p.c"], "c", remove_failed_points=True)


@pytest.mark.parametrize("policy", ["mask", "drop"])
def test_actual_renderer_lines_never_bridge_invalid_coordinate(plot_module, tmp_path, monkeypatch, policy):
    import EFTFit.Fitter.scan_1d_plot as renderer
    scan = invalid_scan([-2, -1, 0, 1, 2], [2, 0, 9990, 0, 2], failed_point_policy=policy)
    captured = []
    original = renderer._save
    def capture(figure, name, display):
        captured.append(figure)
        return original(figure, name, display)
    monkeypatch.setattr(renderer, "_save", capture)
    display = renderer.DisplayConfig.from_mapping({"formats": ["png"], "output_directory": str(tmp_path)})
    result = renderer.render_curves([scan], ["invalid fixture"], display, policy)
    lines = [line for line in captured[0].axes[0].lines if line.get_marker() == "."]
    assert [list(line.get_xdata()) for line in lines] == [[-2, -1], [1, 2]]
    assert all(19980 not in line.get_ydata() for line in lines)
    markers = [line for line in captured[0].axes[0].lines if line.get_marker() == "x"]
    assert bool(markers) == (policy == "mask")
    assert Path(result["paths"][0]).stat().st_size > 0
    renderer.render_summary([("c", scan, scan)], ["p", "f"], display, "summary_" + policy)
    for line in captured[1].axes[0].lines:
        values = line.get_xdata()
        if len(values) == 2 and line.get_linestyle() != "None":
            assert not min(values) < 0 < max(values)


def test_one_semantic_cli_control_and_no_interval_sentinel(plot_module):
    spec = importlib.util.spec_from_file_location("invalid_parse_nll", fitter_dir / "scripts/parse_nll.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parser = module.argument_parser()
    actions = [action for action in parser._actions if "failed" in action.dest]
    assert len(actions) == 1
    assert actions[0].option_strings == ["--failed-point-policy"]
    assert actions[0].choices == ("mask", "drop", "error") and actions[0].default == "mask"
    for path in (fitter_dir / "python/scan_1d.py", fitter_dir / "python/scan_1d_plot.py",
                 fitter_dir / "scripts/EFTPlotter.py", fitter_dir / "scripts/parse_nll.py",
                 fitter_dir / "condor/2sigma_struct_maker_wrapper.py"):
        tree = ast.parse(path.read_text())
        assert not any(isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
                       and abs(node.value) == 999 for node in ast.walk(tree))
        assert "legacy_interval_row" not in path.read_text()
    documentation = (fitter_dir.parent / "docs/how_to/run_configured_1d.md").read_text()
    assert "--failed-point-policy {mask,drop,error}" in documentation
    assert "--remove-failed-points" not in documentation


def test_special_valid_duplicate_repairs_existing_grid_coordinate():
    scan = scan_from_columns({"c": [-1, 0, 0, 1], "deltaNLL": [2, 9990, 0, 2],
                              "quantileExpected": [0, 0, -1, 0]}, "c")
    assert [point.coordinate for point in scan.curve_points] == [-1, 0, 1]
    assert scan.invalid_coordinates == ()
    assert bounds(scan.intervals(1)) == [(-.25, .25)]
    assert len(scan.invalid_entries) == 1


def test_invalid_tracked_values_remain_evidence_and_policy_does_not_change_inference():
    columns = {"c": [-3, -2, -1, 0, 1, 2, 3], "deltaNLL": [9990, 2, .5, 0, .5, 2, 9990],
               "quantileExpected": [0] * 7, "trackedParam_other": [math.nan, 0, 0, 0, 0, 0, math.inf]}
    masked = scan_from_columns(columns, "c")
    dropped = scan_from_columns(columns, "c", failed_point_policy="drop")
    assert asdict(masked.intervals(1)) == asdict(dropped.intervals(1))
    assert masked.intervals(1).inference_status == "USABLE_WITH_INVALID_TAIL_POINTS"
    assert masked.failed_points[0]["tracked_parameters"]["trackedParam_other"] == "nan"
    json.dumps(masked.failed_points, allow_nan=False)


@pytest.mark.parametrize("policy", ["mask", "drop", "error"])
def test_cli_emits_policy_points_topology_and_recovery(plot_module, tmp_path, monkeypatch, capsys, policy):
    import sys
    configured_plotter(plot_module, tmp_path)
    path = write_root(tmp_path / "cli.root", {"c": [-2, -1, 0, 1, 2],
                      "deltaNLL": [2, 0, 9990, 0, 2], "quantileExpected": [0] * 5})
    spec = importlib.util.spec_from_file_location("cli_parse_nll", fitter_dir / "scripts/parse_nll.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "find_root_file_path", lambda *args: path)
    monkeypatch.setattr(sys, "argv", ["parse_nll.py", "--fit-config", str(tmp_path / "fit.yml"),
                                     "--basename", "profiled", "--failed-point-policy", policy])
    if policy == "error":
        with pytest.raises(ValueError, match="invalid evaluation"):
            module.main()
        assert capsys.readouterr().out == ""
    else:
        module.main()
        result = json.loads(capsys.readouterr().out)[0]
        assert result["curve_segments"] == [[-2, -1], [1, 2]]
        assert len(result["points"]) == (5 if policy == "mask" else 4)
        assert result["failed_points"][0]["coordinate"] == 0
        assert result["failed_points"][0]["family"] == "profiled"
        assert all(point["twice_delta_nll"] != 19980 for point in result["points"])


def test_retained_best_coordinate_caller_uses_structured_results(plot_module, tmp_path, monkeypatch):
    plotter = configured_plotter(plot_module, tmp_path)
    good = invalid_scan()
    monkeypatch.setattr(plotter, "getIntervalFits", lambda **kwargs: [{"scan": good, "intervals": good.intervals(4)}])
    calls = []
    def grid(name, wc, fits):
        calls.append(fits)
        return [(wc, fits[wc])]
    monkeypatch.setattr(plotter, "grid2DWC", grid)
    plotter.batchGrid2DWC([".profiled"])
    assert calls == [{"c": 0}]
    bad = invalid_scan([-1, 0, 1], [0, 9990, 0])
    monkeypatch.setattr(plotter, "getIntervalFits", lambda **kwargs: [{"scan": bad}])
    with pytest.raises(ValueError, match="one resolved"):
        plotter.batchGrid2DWC([".profiled"])
    retired = fitter_dir / "condor/2sigma_struct_maker_wrapper.py"
    spec = importlib.util.spec_from_file_location("retired_struct", retired)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit, match="retired"):
        module.main()


def test_repaired_special_failure_does_not_poison_minimum():
    scan = scan_from_columns({"c": [-1, 0, 0, 0, 1], "deltaNLL": [2, 0, 0, 9990, 2],
                              "quantileExpected": [0, 0, -1, -1, 0]}, "c")
    assert scan.minimum_status == "RESOLVED_ON_VALID_DATA"
    assert scan.intervals(1).inference_status == "VALID_COMPLETE"
    assert len(scan.failed_points) == 1


def test_unknown_invalid_coordinate_degrades_nesting_without_guessing_location():
    profiled = invalid_scan([-2, -1, 0, 1, float("nan")], [2, 1, 0, 1, 2], family="profiled")
    frozen = invalid_scan([-2, -1, 0, 1, 2], [3, 1, 0, 1, 3], family="frozen")
    result = nesting_differences(profiled, frozen, profiled_reference_nll=0, frozen_reference_nll=0)
    assert result["status"] == "UNRESOLVED" and len(result["differences"]) == 4
    assert result["unavailable"][0] == {"coordinate": 2, "reason": "NO_VALID_COUNTERPART"}
    assert result["unavailable"][1]["coordinate"] is None
    assert result["unavailable"][1]["family"] == "profiled"

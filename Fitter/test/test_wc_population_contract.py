from pathlib import Path
import sys

import pytest
import yaml


FITTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FITTER_DIR / "python"))

from wc_fit_config import (  # noqa: E402
    build_configured_grid_scan_command,
    load_fit_config,
    resolve_scan_wcs,
)


def write_config(tmp_path, scientific=None, fixed=None, ranges=None):
    scientific = scientific or ["c_one", "c_two", "c_three"]
    fixed = fixed or ["c_fixed_one", "c_fixed_two"]
    ranges = ranges or {
        "c_one": [-1.0, 1.0],
        "c_two": [-2.0, 2.0],
        "c_three": [-3.0, 3.0],
    }
    path = tmp_path / "fit.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "analysis": "TEST_ANALYSIS",
                "wc_population": {
                    "scientific": scientific,
                    "fixed_zero": fixed,
                },
                "wc_ranges": ranges,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def option_values(argv, option):
    return [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == option]


def parse_assignments(value):
    return dict(assignment.split("=", 1) for assignment in value.split(","))


def parse_ranges(value):
    ranges = {}
    for assignment in value.split(":"):
        parameter, bounds = assignment.split("=", 1)
        ranges[parameter] = tuple(float(bound) for bound in bounds.split(","))
    return ranges


def test_profiled_one_wc_materializes_configured_population(tmp_path):
    config_path = write_config(tmp_path)
    argv = build_configured_grid_scan_command(
        config_path,
        "workspace.root",
        scan_params=["c_two"],
        params_tracked=["c_one", "c_three"],
        points=11,
        other=[
            "--freezeParameters",
            "nuisance_alpha,c_fixed_one",
            "--setParameters",
            "nuisance_beta=2,c_fixed_two=0",
        ],
    )

    assert option_values(argv, "-P") == ["c_two"]
    assert option_values(argv, "--redefineSignalPOIs") == ["c_one,c_two,c_three"]
    assert option_values(argv, "--floatOtherPOIs") == ["1"]
    assert option_values(argv, "--freezeParameters") == [
        "c_fixed_one,c_fixed_two,nuisance_alpha"
    ]
    assignments = parse_assignments(option_values(argv, "--setParameters")[0])
    assert assignments == {
        "c_one": "0",
        "c_two": "0",
        "c_three": "0",
        "c_fixed_one": "0",
        "c_fixed_two": "0",
        "nuisance_beta": "2",
    }
    assert argv.count("--setParameterRanges") == 1
    assert parse_ranges(option_values(argv, "--setParameterRanges")[0]) == {
        "c_one": (-1.0, 1.0),
        "c_two": (-2.0, 2.0),
        "c_three": (-3.0, 3.0),
    }
    assert str(config_path) not in argv


def test_frozen_one_wc_retains_full_modelconfig_poi_view(tmp_path):
    config_path = write_config(tmp_path)
    argv = build_configured_grid_scan_command(
        config_path,
        "workspace.root",
        freeze=True,
        scan_params=["c_two"],
    )

    assert option_values(argv, "-P") == ["c_two"]
    assert option_values(argv, "--redefineSignalPOIs") == ["c_one,c_two,c_three"]
    assert option_values(argv, "--floatOtherPOIs") == []
    assert option_values(argv, "--freezeParameters") == ["c_fixed_one,c_fixed_two"]
    assignments = parse_assignments(option_values(argv, "--setParameters")[0])
    assert assignments["c_one"] == assignments["c_three"] == "0"
    assert parse_ranges(option_values(argv, "--setParameterRanges")[0]) == {
        "c_one": (-1.0, 1.0),
        "c_two": (-2.0, 2.0),
        "c_three": (-3.0, 3.0),
    }


def test_scan_wcs_default_and_strict_subset(tmp_path):
    config = load_fit_config(write_config(tmp_path))
    assert resolve_scan_wcs(config) == config.scientific_wcs
    assert resolve_scan_wcs(config, ["c_two"]) == ("c_two",)


def test_invalid_scan_wc_is_rejected(tmp_path):
    config = load_fit_config(write_config(tmp_path))
    with pytest.raises(ValueError, match="subset"):
        resolve_scan_wcs(config, ["not_configured"])


def test_range_map_order_is_not_semantic(tmp_path):
    config = load_fit_config(
        write_config(
            tmp_path,
            ranges={
                "c_three": [-3.0, 3.0],
                "c_one": [-1.0, 1.0],
                "c_two": [-2.0, 2.0],
            },
        )
    )
    assert list(config.wc_ranges) == ["c_one", "c_two", "c_three"]


@pytest.mark.parametrize(
    "ranges,match",
    [
        (
            {"c_one": [-1.0, 1.0], "c_two": [-2.0, 2.0]},
            "exactly match",
        ),
        (
            {
                "c_one": [-1.0, 1.0],
                "c_two": [-2.0, 2.0],
                "c_three": [-3.0, 3.0],
                "c_extra": [-4.0, 4.0],
            },
            "exactly match",
        ),
        (
            {
                "c_one": ["low", 1.0],
                "c_two": [-2.0, 2.0],
                "c_three": [-3.0, 3.0],
            },
            "finite numbers",
        ),
        (
            {
                "c_one": [float("nan"), 1.0],
                "c_two": [-2.0, 2.0],
                "c_three": [-3.0, 3.0],
            },
            "finite numbers",
        ),
        (
            {
                "c_one": [1.0, 1.0],
                "c_two": [-2.0, 2.0],
                "c_three": [-3.0, 3.0],
            },
            "below upper",
        ),
        (
            {
                "c_one": [False, 1.0],
                "c_two": [-2.0, 2.0],
                "c_three": [-3.0, 3.0],
            },
            "finite numbers",
        ),
    ],
)
def test_invalid_range_maps_are_rejected(tmp_path, ranges, match):
    with pytest.raises(ValueError, match=match):
        load_fit_config(write_config(tmp_path, ranges=ranges))


@pytest.mark.parametrize(
    "scientific,fixed,match",
    [
        (["c_one", "c_one"], ["c_fixed"], "duplicate"),
        (["c_one"], ["c_fixed", "c_fixed"], "duplicate"),
        (["c_one", "shared"], ["shared"], "overlap"),
    ],
)
def test_duplicate_and_overlapping_populations_are_rejected(
    tmp_path, scientific, fixed, match
):
    with pytest.raises(ValueError, match=match):
        load_fit_config(write_config(tmp_path, scientific, fixed))


def test_nonzero_fixed_wc_assignment_is_rejected(tmp_path):
    config_path = write_config(tmp_path)
    with pytest.raises(ValueError, match="must equal zero"):
        build_configured_grid_scan_command(
            config_path,
            "workspace.root",
            scan_params=["c_one"],
            other=["--setParameters", "c_fixed_one=1"],
        )


def test_nonzero_scientific_starting_value_is_rejected(tmp_path):
    config_path = write_config(tmp_path)
    with pytest.raises(ValueError, match="not canonical"):
        build_configured_grid_scan_command(
            config_path,
            "workspace.root",
            scan_params=["c_one"],
            other=["--setParameters", "c_two=0.5"],
        )


def test_conflicting_redefine_signal_pois_is_rejected(tmp_path):
    config_path = write_config(tmp_path)
    with pytest.raises(ValueError, match="redefineSignalPOIs"):
        build_configured_grid_scan_command(
            config_path,
            "workspace.root",
            scan_params=["c_one"],
            other=["--redefineSignalPOIs", "c_one"],
        )


@pytest.mark.parametrize(
    "freeze,value",
    [(False, "0"), (True, "1")],
)
def test_contradictory_float_other_pois_is_rejected(tmp_path, freeze, value):
    config_path = write_config(tmp_path)
    with pytest.raises(ValueError, match="floatOtherPOIs"):
        build_configured_grid_scan_command(
            config_path,
            "workspace.root",
            freeze=freeze,
            scan_params=["c_one"],
            other=["--floatOtherPOIs", value],
        )


def test_scientific_wc_freeze_collision_is_rejected(tmp_path):
    config_path = write_config(tmp_path)
    with pytest.raises(ValueError, match="freeze scientific"):
        build_configured_grid_scan_command(
            config_path,
            "workspace.root",
            scan_params=["c_one"],
            other=["--freezeParameters", "c_two"],
        )


def test_scientific_wc_regex_freeze_collision_is_rejected(tmp_path):
    config_path = write_config(tmp_path)
    with pytest.raises(ValueError, match="freeze scientific"):
        build_configured_grid_scan_command(
            config_path,
            "workspace.root",
            scan_params=["c_one"],
            other=["--freezeParameters", "rgx{c_.*}"],
        )


def test_consistent_caller_controls_are_canonicalized_once(tmp_path):
    config_path = write_config(tmp_path)
    argv = build_configured_grid_scan_command(
        config_path,
        "workspace.root",
        scan_params=["c_one"],
        other=[
            "--redefineSignalPOIs",
            "c_three,c_one,c_two",
            "--floatOtherPOIs=1",
            "--freezeParameters",
            "c_fixed_one,nuisance_alpha",
            "--freezeParameters",
            "c_fixed_two,nuisance_alpha",
            "--setParameters",
            "c_fixed_one=0,nuisance_beta=2",
            "--setParameters",
            "c_two=0,nuisance_beta=2",
        ],
    )

    for option in (
        "--redefineSignalPOIs",
        "--floatOtherPOIs",
        "--freezeParameters",
        "--setParameterRanges",
        "--setParameters",
    ):
        assert argv.count(option) == 1
    assert "nuisance_alpha" in option_values(argv, "--freezeParameters")[0]
    assert parse_assignments(option_values(argv, "--setParameters")[0])["nuisance_beta"] == "2"


def test_scan_subset_does_not_narrow_configured_range_map(tmp_path):
    config_path = write_config(tmp_path)
    first = build_configured_grid_scan_command(
        config_path,
        "workspace.root",
        scan_params=["c_one"],
    )
    second = build_configured_grid_scan_command(
        config_path,
        "workspace.root",
        scan_params=["c_two"],
    )
    assert option_values(first, "--setParameterRanges") == option_values(
        second, "--setParameterRanges"
    )


def test_equal_scientific_and_unrelated_ranges_are_canonicalized_once(tmp_path):
    config_path = write_config(tmp_path)
    argv = build_configured_grid_scan_command(
        config_path,
        "workspace.root",
        scan_params=["c_one"],
        other=[
            "--setParameterRanges",
            "c_two=-2,2:nuisance_gamma=0,5",
        ],
    )
    assert argv.count("--setParameterRanges") == 1
    assert parse_ranges(option_values(argv, "--setParameterRanges")[0]) == {
        "c_one": (-1.0, 1.0),
        "c_two": (-2.0, 2.0),
        "c_three": (-3.0, 3.0),
        "nuisance_gamma": (0.0, 5.0),
    }


def test_conflicting_scientific_range_is_rejected(tmp_path):
    config_path = write_config(tmp_path)
    with pytest.raises(ValueError, match="contradicts configured range"):
        build_configured_grid_scan_command(
            config_path,
            "workspace.root",
            scan_params=["c_one"],
            other=["--setParameterRanges", "c_two=-4,4"],
        )


def test_scientific_range_regex_collision_is_rejected(tmp_path):
    config_path = write_config(tmp_path)
    with pytest.raises(ValueError, match="regex collides"):
        build_configured_grid_scan_command(
            config_path,
            "workspace.root",
            scan_params=["c_one"],
            other=["--setParameterRanges", "rgx{c_.*}=-1,1"],
        )

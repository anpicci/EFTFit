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


def write_config(tmp_path, scientific=None, fixed=None):
    path = tmp_path / "fit.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "analysis": "TEST_ANALYSIS",
                "wc_population": {
                    "scientific": scientific or ["c_one", "c_two", "c_three"],
                    "fixed_zero": fixed or ["c_fixed_one", "c_fixed_two"],
                },
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


def test_scan_wcs_default_and_strict_subset(tmp_path):
    config = load_fit_config(write_config(tmp_path))
    assert resolve_scan_wcs(config) == config.scientific_wcs
    assert resolve_scan_wcs(config, ["c_two"]) == ("c_two",)


def test_invalid_scan_wc_is_rejected(tmp_path):
    config = load_fit_config(write_config(tmp_path))
    with pytest.raises(ValueError, match="subset"):
        resolve_scan_wcs(config, ["not_configured"])


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
        "--setParameters",
    ):
        assert argv.count(option) == 1
    assert "nuisance_alpha" in option_values(argv, "--freezeParameters")[0]
    assert parse_assignments(option_values(argv, "--setParameters")[0])["nuisance_beta"] == "2"


def test_real_top_26_006_subset_keeps_all_scientific_runtime_pois():
    workspace_root = Path(__file__).resolve().parents[6]
    config_path = workspace_root / "topeft/topeft/params/fits/TOP_26_006.yml"
    config = load_fit_config(config_path)
    argv = build_configured_grid_scan_command(
        config_path,
        "historical_renderer_fixture.root",
        scan_params=["ctW"],
    )

    runtime_pois = option_values(argv, "--redefineSignalPOIs")[0].split(",")
    assert len(config.scientific_wcs) == 26
    assert len(config.fixed_wcs) == 10
    assert set(runtime_pois) == set(config.scientific_wcs)
    assert not set(runtime_pois) & set(config.fixed_wcs)
    assert option_values(argv, "-P") == ["ctW"]
    assert str(config_path) not in " ".join(argv)


def test_eftfitter_configured_path_uses_pure_builder():
    source = (FITTER_DIR / "scripts/EFTFitter.py").read_text(encoding="utf-8")
    assert "build_configured_grid_scan_command(" in source
    assert "fit_config_path=fit_config" in source
    assert "if fit_config is not None:" in source
    assert "wc_val is not supported with configured WC populations" in source

"""Pure package intake and maintained 1D dispatch, before model construction."""

import ast
import io
import itertools
import logging
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest
import yaml


FITTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FITTER_DIR / "python"))

import wc_fit_config  # noqa: E402
from wc_fit_config import (  # noqa: E402
    WCFitConfig,
    build_configured_grid_scan_command,
    load_fit_config,
    load_fit_input_package,
    resolve_scan_wcs,
)


def write_package(root):
    datacards = root / "datacards"
    datacards.mkdir(parents=True)
    config_path = root / "config" / "TOP_26_006.yml"
    config_path.parent.mkdir()
    config_path.write_text(
        yaml.safe_dump(
            {
                "analysis": "SYNTHETIC",
                "wc_population": {
                    "scientific": ["c_alpha", "c_beta"],
                    "fixed_zero": ["c_fixed"],
                },
                "wc_ranges": {"c_alpha": [-1, 2], "c_beta": [-3, 4]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return root


def load_batch_method(name="batch1DScanEFT"):
    script_path = FITTER_DIR / "scripts" / "EFTFitter.py"
    tree = ast.parse(script_path.read_text(encoding="utf-8"))
    fitter_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "EFTFit"
    )
    method = next(
        node for node in fitter_class.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    namespace = {
        "WCFitConfig": WCFitConfig,
        "load_fit_config": load_fit_config,
        "load_fit_input_package": load_fit_input_package,
        "resolve_scan_wcs": resolve_scan_wcs,
        "itertools": itertools,
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(script_path), "exec"), namespace)
    return namespace[name]


def load_grid_scan_method(namespace):
    script_path = FITTER_DIR / "scripts" / "EFTFitter.py"
    tree = ast.parse(script_path.read_text(encoding="utf-8"))
    fitter_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "EFTFit"
    )
    method = next(
        node for node in fitter_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "gridScan"
    )
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(script_path), "exec"), namespace)
    return namespace["gridScan"]


class RecordingFitter:
    batch1DScanEFT = load_batch_method()
    batch2DScanEFT = load_batch_method("batch2DScanEFT")
    batch3DScanEFT = load_batch_method("batch3DScanEFT")

    def __init__(self):
        self.wcs = ["legacy_wc"]
        self.wc_ranges_njets = {"legacy_wc": (-9, 9)}
        self.wc_ranges_differential = {"legacy_wc": (-8, 8)}
        self.grid_scan_calls = []

    def gridScan(self, *args, **kwargs):
        self.grid_scan_calls.append((args, kwargs))


@pytest.mark.parametrize("selection", [{}, {"fit_config": None}])
def test_batch_requires_explicit_fit_input_selection(selection):
    fitter = RecordingFitter()
    with pytest.raises(ValueError, match="fit input/config selection must be explicit"):
        fitter.batch1DScanEFT(**selection)
    assert fitter.grid_scan_calls == []


@pytest.mark.parametrize("selection", [{}, {"fit_config": None}])
def test_grid_scan_requires_selection_before_workspace_lookup(selection):
    def unexpected_workspace_lookup(_name):
        pytest.fail("workspace lookup occurred before fit input selection")

    grid_scan = load_grid_scan_method({
        "logging": logging,
        "os": SimpleNamespace(getenv=unexpected_workspace_lookup),
    })
    with pytest.raises(ValueError, match="fit input/config selection must be explicit"):
        grid_scan(SimpleNamespace(), **selection)


def test_grid_scan_explicit_legacy_uses_legacy_command(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace.root"
    workspace.write_bytes(b"synthetic workspace placeholder")
    monkeypatch.setenv("CMSSW_BASE", str(tmp_path))
    popen_calls = []
    call_calls = []

    def record_popen(argv, **_kwargs):
        popen_calls.append(list(argv))
        return SimpleNamespace(stdout=io.BytesIO(), stderr=io.BytesIO(), wait=lambda: 0)

    grid_scan = load_grid_scan_method({
        "logging": logging,
        "os": os,
        "sp": SimpleNamespace(
            PIPE=object(),
            Popen=record_popen,
            call=lambda argv: call_calls.append(list(argv)),
        ),
    })
    fitter = SimpleNamespace(log_subprocess_output=lambda *_args: None)
    grid_scan(
        fitter,
        fit_config="legacy",
        workspace=str(workspace),
        scan_params=["legacy_wc"],
        points=7,
    )
    assert len(popen_calls) == 1
    assert popen_calls[0][:7] == [
        "combineTool.py", "-d", str(workspace), "-M", "MultiDimFit", "--algo", "grid"
    ]
    assert popen_calls[0][popen_calls[0].index("-P") + 1] == "legacy_wc"
    assert popen_calls[0][popen_calls[0].index("--points") + 1] == "7"
    assert call_calls[0][0] == "mv"


@pytest.mark.parametrize("freeze", [False, True])
def test_package_config_reaches_pure_configured_command(tmp_path, freeze):
    package = load_fit_input_package(write_package(tmp_path / "package"))
    assert isinstance(package.fit_config, WCFitConfig)
    argv = build_configured_grid_scan_command(
        fit_config_path=package.fit_config,
        workspace="synthetic_workspace.root",
        freeze=freeze,
        scan_params=["c_alpha"],
        params_tracked=["c_beta"],
        points=7,
    )

    def option_values(option):
        return [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == option]

    assert option_values("-P") == ["c_alpha"]
    assert option_values("--redefineSignalPOIs") == ["c_alpha,c_beta"]
    assert option_values("--trackParameters") == ["c_beta"]
    assert option_values("--setParameters") == ["c_alpha=0,c_beta=0,c_fixed=0"]
    assert option_values("--freezeParameters") == ["c_fixed"]
    assert option_values("--setParameterRanges") == ["c_alpha=-1,2:c_beta=-3,4"]
    assert option_values("--floatOtherPOIs") == ([] if freeze else ["1"])
    assert "legacy_wc" not in " ".join(argv)


def test_package_config_is_authority_and_dispatch_reuses_it(tmp_path):
    package_root = write_package(tmp_path / "package")
    (package_root / "datacards" / "unrelated.txt").write_text("extra", encoding="utf-8")
    package = load_fit_input_package(package_root)
    assert package.datacards == package_root / "datacards"
    assert package.config_path == package_root / "config" / "TOP_26_006.yml"
    assert package.fit_config.scientific_wcs == ("c_alpha", "c_beta")
    assert package.fit_config.fixed_wcs == ("c_fixed",)
    assert package.fit_config.wc_ranges == {
        "c_alpha": (-1, 2), "c_beta": (-3, 4)
    }

    fitter = RecordingFitter()
    fitter.batch1DScanEFT(fit_config="package", fit_package_root=package_root)
    assert len(fitter.grid_scan_calls) == 2
    for arguments, keywords in fitter.grid_scan_calls:
        assert isinstance(keywords["fit_config"], WCFitConfig)
        assert keywords["fit_config"] == package.fit_config
        assert keywords["execution_provenance"] is True
        assert "legacy_wc" not in arguments[4]
    assert list(package_root.rglob("selectedWCs.txt")) == []


def test_relocated_package_needs_no_source_checkout_or_storage(tmp_path):
    source = write_package(tmp_path / "first" / "package")
    relocated = tmp_path / "second" / "package"
    shutil.copytree(source, relocated)
    first = load_fit_input_package(source)
    second = load_fit_input_package(relocated)
    assert first.fit_config == second.fit_config
    assert second.datacards == relocated / "datacards"
    assert second.config_path == relocated / "config" / "TOP_26_006.yml"


@pytest.mark.parametrize(
    "missing", ["root", "root_file", "datacards", "config", "malformed", "fields"]
)
def test_package_input_failure_never_dispatches_legacy(tmp_path, missing):
    package_root = write_package(tmp_path / "package")
    if missing == "root":
        package_root = tmp_path / "absent"
    elif missing == "root_file":
        package_root = tmp_path / "file"
        package_root.write_text("file", encoding="utf-8")
    elif missing == "datacards":
        (package_root / "datacards").rmdir()
    elif missing == "config":
        (package_root / "config" / "TOP_26_006.yml").unlink()
    else:
        (package_root / "config" / "TOP_26_006.yml").write_text(
            "wc_population: [" if missing == "malformed" else "analysis: SYNTHETIC\n",
            encoding="utf-8",
        )

    fitter = RecordingFitter()
    with pytest.raises(ValueError, match="fit package"):
        fitter.batch1DScanEFT(fit_config="package", fit_package_root=package_root)
    assert fitter.grid_scan_calls == []


def test_unreadable_config_fails_as_package_error(tmp_path, monkeypatch):
    package_root = write_package(tmp_path / "package")

    def unreadable_config(_path):
        raise PermissionError("permission denied")

    monkeypatch.setattr(wc_fit_config, "load_fit_config", unreadable_config)
    with pytest.raises(ValueError, match="fit package config is invalid: permission denied"):
        load_fit_input_package(package_root)


def test_package_input_cannot_redirect_outside_root(tmp_path):
    package_root = write_package(tmp_path / "package")
    outside = tmp_path / "outside"
    outside.mkdir()
    (package_root / "datacards").rmdir()
    (package_root / "datacards").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside package root"):
        load_fit_input_package(package_root)


def test_package_requires_explicit_root_and_legacy_is_separate(tmp_path):
    fitter = RecordingFitter()
    with pytest.raises(ValueError, match="fit package root is required"):
        fitter.batch1DScanEFT(fit_config="package")
    assert fitter.grid_scan_calls == []
    with pytest.raises(ValueError, match="requires package mode"):
        fitter.batch1DScanEFT(fit_config="legacy", fit_package_root=tmp_path)
    fitter.batch1DScanEFT(fit_config="legacy", scan_wcs=["legacy_wc"])
    assert len(fitter.grid_scan_calls) == 1
    assert fitter.grid_scan_calls[0][0][6][0] == "--setParameterRanges legacy_wc=-9,9"


def test_manifest_provenance_and_snapshot_do_not_gate_science(tmp_path):
    package_root = write_package(tmp_path / "package")
    manifest = package_root / "fit-input-manifest.json"
    before = load_fit_input_package(package_root).fit_config
    manifest.write_text(
        '{"package_creation":{"topeft_commit":"old"},'
        '"datacards":{"input_snapshot_sha256":"first"}}',
        encoding="utf-8",
    )
    first = load_fit_input_package(package_root).fit_config
    manifest.write_text(
        '{"package_creation":{"topeft_commit":"new"},'
        '"datacards":{"input_snapshot_sha256":"second"}}',
        encoding="utf-8",
    )
    assert first == before == load_fit_input_package(package_root).fit_config


def write_multidimensional_package(root):
    package_root = write_package(root)
    config_path = package_root / "config" / "TOP_26_006.yml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "analysis": "SYNTHETIC",
                "wc_population": {
                    "scientific": ["c_alpha", "c_beta", "c_gamma", "c_delta"],
                    "fixed_zero": ["c_fixed"],
                },
                "wc_ranges": {
                    "c_alpha": [-1, 2],
                    "c_beta": [-3, 4],
                    "c_gamma": [-5, 6],
                    "c_delta": [-7, 8],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return package_root


def configured_argv(call):
    _, keywords = call
    return build_configured_grid_scan_command(
        fit_config_path=keywords["fit_config"],
        workspace=keywords["workspace"],
        name=keywords["name"],
        freeze=keywords["freeze"],
        scan_params=keywords["scan_params"],
        params_tracked=keywords["params_tracked"],
        points=keywords["points"],
        other=keywords["other"],
    )


def option_value(argv, option):
    return argv[argv.index(option) + 1]


@pytest.mark.parametrize("method", ["batch2DScanEFT", "batch3DScanEFT"])
@pytest.mark.parametrize("selection", [{}, {"fit_config": None}])
def test_multidimensional_selection_fails_closed(method, selection):
    fitter = RecordingFitter()
    with pytest.raises(ValueError, match="fit input/config selection must be explicit"):
        getattr(fitter, method)(**selection)
    assert fitter.grid_scan_calls == []


@pytest.mark.parametrize("freeze", [False, True])
def test_configured_2d_package_dispatch_uses_full_population(tmp_path, freeze):
    package_root = write_multidimensional_package(tmp_path / "package")
    fitter = RecordingFitter()
    fitter.batch2DScanEFT(
        fit_config="package",
        fit_package_root=package_root,
        wcs=[("c_alpha", "c_beta")],
        freeze=freeze,
        workspace="synthetic_workspace.root",
        points=11,
    )
    assert len(fitter.grid_scan_calls) == 1
    _, keywords = fitter.grid_scan_calls[0]
    assert isinstance(keywords["fit_config"], WCFitConfig)
    assert keywords["params_tracked"] == ["c_gamma", "c_delta"]
    assert keywords["execution_provenance"] is True
    argv = configured_argv(fitter.grid_scan_calls[0])
    assert [argv[index + 1] for index, token in enumerate(argv[:-1]) if token == "-P"] == [
        "c_alpha", "c_beta"
    ]
    assert option_value(argv, "--redefineSignalPOIs") == "c_alpha,c_beta,c_gamma,c_delta"
    assert option_value(argv, "--trackParameters") == "c_gamma,c_delta"
    assert option_value(argv, "--setParameterRanges") == (
        "c_alpha=-1,2:c_beta=-3,4:c_gamma=-5,6:c_delta=-7,8"
    )
    assert option_value(argv, "--setParameters") == (
        "c_alpha=0,c_beta=0,c_gamma=0,c_delta=0,c_fixed=0"
    )
    assert option_value(argv, "--freezeParameters") == "c_fixed"
    assert ("--floatOtherPOIs" in argv) is not freeze
    assert "legacy_wc" not in " ".join(argv)


@pytest.mark.parametrize("pair", [
    ("c_alpha", "c_alpha"),
    ("c_alpha", "not_scientific"),
    ("c_alpha", "c_fixed"),
])
def test_configured_2d_rejects_invalid_axes(tmp_path, pair):
    config_path = write_multidimensional_package(tmp_path / "package") / "config" / "TOP_26_006.yml"
    fitter = RecordingFitter()
    with pytest.raises(ValueError):
        fitter.batch2DScanEFT(fit_config=config_path, wcs=[pair])
    assert fitter.grid_scan_calls == []


def test_configured_2d_unique_pair_enumeration(tmp_path):
    config = load_fit_input_package(write_multidimensional_package(tmp_path / "package")).fit_config
    fitter = RecordingFitter()
    fitter.batch2DScanEFT(fit_config=config, allPairs=True, wcs=["c_delta", "c_alpha", "c_beta"])
    assert [call[1]["scan_params"] for call in fitter.grid_scan_calls] == [
        ["c_delta", "c_alpha"], ["c_delta", "c_beta"], ["c_alpha", "c_beta"]
    ]
    assert all(call[1]["fit_config"].scientific_wcs == config.scientific_wcs for call in fitter.grid_scan_calls)


def test_configured_2d_validates_all_pairs_before_dispatch(tmp_path):
    config_path = write_multidimensional_package(tmp_path / "package") / "config" / "TOP_26_006.yml"
    fitter = RecordingFitter()
    with pytest.raises(ValueError, match="subset"):
        fitter.batch2DScanEFT(
            fit_config=config_path,
            wcs=[("c_alpha", "c_beta"), ("c_alpha", "not_scientific")],
        )
    assert fitter.grid_scan_calls == []


def test_legacy_2d_selection_preserves_range_table():
    fitter = RecordingFitter()
    fitter.wcs = ["legacy_wc", "second_wc"]
    fitter.wc_ranges_differential["second_wc"] = (-6, 6)
    fitter.batch2DScanEFT(fit_config="legacy", wcs=[("legacy_wc", "second_wc")])
    assert fitter.grid_scan_calls[0][1]["fit_config"] == "legacy"
    assert fitter.grid_scan_calls[0][1]["other"][0] == (
        "--setParameterRanges legacy_wc=-8,8:second_wc=-6,6"
    )


def test_configured_3d_package_dispatch_uses_existing_grid_seam(tmp_path):
    package_root = write_multidimensional_package(tmp_path / "package")
    fitter = RecordingFitter()
    fitter.batch3DScanEFT(
        fit_config="package", fit_package_root=package_root,
        wc_triplet=[("c_alpha", "c_beta", "c_gamma")],
        workspace="synthetic_workspace.root", points=13,
    )
    assert len(fitter.grid_scan_calls) == 1
    _, keywords = fitter.grid_scan_calls[0]
    assert isinstance(keywords["fit_config"], WCFitConfig)
    assert keywords["params_tracked"] == ["c_delta"]
    assert keywords["execution_provenance"] is True
    argv = configured_argv(fitter.grid_scan_calls[0])
    assert option_value(argv, "--redefineSignalPOIs") == "c_alpha,c_beta,c_gamma,c_delta"
    assert option_value(argv, "--setParameterRanges") == (
        "c_alpha=-1,2:c_beta=-3,4:c_gamma=-5,6:c_delta=-7,8"
    )
    assert option_value(argv, "--freezeParameters") == "c_fixed"


@pytest.mark.parametrize("triplet", [
    ("c_alpha", "c_beta", "c_beta"),
    ("c_alpha", "c_beta", "not_scientific"),
    ("c_alpha", "c_beta", "c_fixed"),
])
def test_configured_3d_rejects_invalid_axes(tmp_path, triplet):
    config = load_fit_input_package(write_multidimensional_package(tmp_path / "package")).fit_config
    fitter = RecordingFitter()
    with pytest.raises(ValueError):
        fitter.batch3DScanEFT(fit_config=config, wc_triplet=[triplet])
    assert fitter.grid_scan_calls == []


def test_configured_3d_triplet_enumeration_is_deterministic(tmp_path):
    config = load_fit_input_package(write_multidimensional_package(tmp_path / "package")).fit_config
    fitter = RecordingFitter()
    fitter.batch3DScanEFT(fit_config=config, allPairs=True)
    assert [call[1]["scan_params"] for call in fitter.grid_scan_calls] == [
        list(triplet) for triplet in itertools.combinations(config.scientific_wcs, 3)
    ]


def test_configured_3d_path_validates_all_triplets_before_dispatch(tmp_path):
    config_path = write_multidimensional_package(tmp_path / "package") / "config" / "TOP_26_006.yml"
    fitter = RecordingFitter()
    with pytest.raises(ValueError, match="subset"):
        fitter.batch3DScanEFT(
            fit_config=config_path,
            wc_triplet=[
                ("c_alpha", "c_beta", "c_gamma"),
                ("c_alpha", "c_beta", "not_scientific"),
            ],
        )
    assert fitter.grid_scan_calls == []


def test_legacy_3d_selection_preserves_explicit_triplet():
    fitter = RecordingFitter()
    triplet = ("legacy_wc", "second_wc", "third_wc")
    fitter.wcs = list(triplet)
    fitter.batch3DScanEFT(fit_config="legacy", wc_triplet=[triplet])
    assert fitter.grid_scan_calls[0][1]["scan_params"] == list(triplet)
    assert fitter.grid_scan_calls[0][1]["fit_config"] == "legacy"

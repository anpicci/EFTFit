"""Pure package intake and maintained 1D dispatch, before model construction."""

import ast
from pathlib import Path
import shutil
import sys

import pytest
import yaml


FITTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FITTER_DIR / "python"))

import wc_fit_config  # noqa: E402
from wc_fit_config import (  # noqa: E402
    WCFitConfig,
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


def load_batch_method():
    script_path = FITTER_DIR / "scripts" / "EFTFitter.py"
    tree = ast.parse(script_path.read_text(encoding="utf-8"))
    fitter_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "EFTFit"
    )
    method = next(
        node for node in fitter_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "batch1DScanEFT"
    )
    namespace = {
        "WCFitConfig": WCFitConfig,
        "load_fit_config": load_fit_config,
        "load_fit_input_package": load_fit_input_package,
        "resolve_scan_wcs": resolve_scan_wcs,
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(script_path), "exec"), namespace)
    return namespace["batch1DScanEFT"]


class RecordingFitter:
    batch1DScanEFT = load_batch_method()

    def __init__(self):
        self.wcs = ["legacy_wc"]
        self.wc_ranges_njets = {"legacy_wc": (-9, 9)}
        self.grid_scan_calls = []

    def gridScan(self, *args, **kwargs):
        self.grid_scan_calls.append((args, kwargs))


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

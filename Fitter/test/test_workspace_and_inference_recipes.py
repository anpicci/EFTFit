"""Synthetic recipe and argv contracts; no Combine or ROOT execution."""

import ast
from pathlib import Path
import sys

import pytest
import yaml


FITTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FITTER_DIR / "python"))

from inference_recipe import (  # noqa: E402
    bind_inference_invocation,
    normalize_inference_recipe,
)
from wc_fit_config import WCFitConfig  # noqa: E402
import workspace_build_recipe as workspace_builder  # noqa: E402
from workspace_build_recipe import (  # noqa: E402
    WorkspaceBuildConfig,
    build_workspace_argv,
    load_workspace_build_recipe,
    normalize_workspace_build_recipe,
)


@pytest.fixture
def fit_config():
    return WCFitConfig(
        "SYNTHETIC", ("c_one", "c_two", "c_three"), ("extra_one",),
        {"c_one": (-2, 2), "c_two": (-3, 3), "c_three": (-4, 4)},
    )


def test_workspace_yaml_resolves_locators_and_preserves_model_options(tmp_path):
    path = tmp_path / "recipe.yml"
    path.write_text(yaml.safe_dump({
        "schema_version": 1,
        "input": {"combined_card": "cards/combined.txt"},
        "model": {"plugin": "EFTFit.Fitter.EFTModel:eftmodel", "options": ["verbose"]},
        "parameterization": {"option": "fits", "source": "fits.npy"},
        "build": {"channel_masks": True},
        "output": {"workspace": "workspace.root"},
        "software": {"eftfit": "stale_revision_is_provenance"},
    }), encoding="utf-8")
    config = load_workspace_build_recipe(path)
    assert build_workspace_argv(config) == [
        "text2workspace.py", str(tmp_path / "cards/combined.txt"),
        "-P", "EFTFit.Fitter.EFTModel:eftmodel",
        "--PO", "fits={}".format(tmp_path / "fits.npy"),
        "--PO", "verbose", "-o", str(tmp_path / "workspace.root"), "--channel-masks",
    ]


def test_canonical_cli_loads_yaml_without_executing_combine(monkeypatch):
    path = FITTER_DIR.parent / "docs/reference/workspace_build_recipe_v1.example.yml"
    observed = []
    monkeypatch.setattr(workspace_builder, "run_workspace_build", lambda config, overwrite: observed.append((config, overwrite)))
    workspace_builder.main(["--recipe", str(path)])
    assert len(observed) == 1
    assert observed[0][0].plugin == "EFTFit.Fitter.EFTModel:eftmodel"
    assert observed[0][1] is False


@pytest.mark.parametrize("change", [
    {"schema_version": 2},
    {"model": {"plugin": "p", "options": ["wcs=c_one"]}},
    {"model": {"plugin": "p", "options": ["fits=other.npy"]}},
    {"build": {"channel_masks": "yes"}},
    {"job_set_id": "execution_state"},
])
def test_workspace_recipe_rejects_ambiguous_or_unsupported_fields(tmp_path, change):
    recipe = {
        "schema_version": 1, "input": {"combined_card": "card.txt"},
        "model": {"plugin": "p"}, "output": {"workspace": "ws.root"},
    }
    recipe.update(change)
    with pytest.raises(ValueError):
        normalize_workspace_build_recipe(recipe, tmp_path)


def test_legacy_model_variants_use_one_argv_builder():
    shell = WorkspaceBuildConfig(
        "combinedcard.txt", "HiggsAnalysis.CombinedLimit.InterferenceModels:interferenceModel",
        "workspace.root", "scalingData", "scalings.json", ("verbose",),
    )
    eft = WorkspaceBuildConfig(
        "card.txt", "EFTFit.Fitter.EFTModel:eftmodel", "EFTWorkspace.root",
        "fits", "/cmssw/EFT_Parameterization.npy", channel_masks=True,
    )
    helper = WorkspaceBuildConfig(
        "card.txt", "HiggsAnalysis.CombinedLimit.PhysicsModel:multiSignalModel",
        "SMWorkspace.root", model_options=("map=.*/ttH:mu_ttH[1]",),
        allow_no_background=True,
    )
    helper_eft = WorkspaceBuildConfig(
        "card.txt", "EFTFit.Fitter.EFTModel:eftmodel", "16D.root",
        model_options=("fits=/hist/EFT_Parameterization.npy", "wcs=c_one,c_two"),
        allow_no_background=True,
    )
    assert build_workspace_argv(shell)[4:] == [
        "--PO", "scalingData=scalings.json", "--PO", "verbose", "-o", "workspace.root",
    ]
    assert build_workspace_argv(eft)[4:] == [
        "--PO", "fits=/cmssw/EFT_Parameterization.npy", "-o", "EFTWorkspace.root", "--channel-masks",
    ]
    assert build_workspace_argv(helper)[4:] == [
        "--PO", "map=.*/ttH:mu_ttH[1]", "-o", "SMWorkspace.root", "--X-allow-no-background",
    ]
    assert build_workspace_argv(helper_eft)[4:] == [
        "--PO", "fits=/hist/EFT_Parameterization.npy", "--PO", "wcs=c_one,c_two",
        "-o", "16D.root", "--X-allow-no-background",
    ]


def test_maintained_adapters_reference_canonical_builder():
    source = (FITTER_DIR / "python/CombineHelper.py").read_text(encoding="utf-8")
    method = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "make_workspace"
    )
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "build_workspace_argv" for node in ast.walk(method))
    fitter = (FITTER_DIR / "scripts/EFTFitter.py").read_text(encoding="utf-8")
    assert "return run_workspace_build(" in fitter
    shell = (FITTER_DIR / "scripts/text2workspace.sh").read_text(encoding="utf-8")
    assert "workspace_build_recipe.py" in shell


@pytest.mark.parametrize("family,axes,policy,others", [
    ("scan_1d_profiled", ("c_one",), "profiled", 2),
    ("scan_1d_frozen", ("c_one",), "frozen", 2),
    ("scan_2d_profiled", ("c_one", "c_two"), "profiled", 1),
    ("scan_2d_frozen", ("c_one", "c_two"), "frozen", 1),
])
def test_scan_families_bind_only_concrete_axes(fit_config, family, axes, policy, others):
    recipe = normalize_inference_recipe({
        "schema_version": 1, "family": family, "dataset_mode": "observed",
    }, fit_config)
    bound = bind_inference_invocation(recipe, fit_config, axes=axes)
    assert bound["other_scientific_policy"] == policy
    assert len(bound["other_scientific_wcs"]) == others
    assert bound["fixed_zero_wcs"] == ("extra_one",)
    assert bound["wc_ranges"] is fit_config.wc_ranges
    with pytest.raises(ValueError):
        bind_inference_invocation(recipe, fit_config, axes=("c_one",) * len(axes)) if len(axes) == 2 else bind_inference_invocation(recipe, fit_config, axes=())


@pytest.mark.parametrize("family", ["impacts_frozen", "impacts_profiled"])
def test_impacts_reference_default_and_distinct_starts(fit_config, family):
    recipe = normalize_inference_recipe({
        "schema_version": 1, "family": family, "dataset_mode": "asimov",
        "fit_start_values": {"c_one": 1.5},
    }, fit_config)
    assert recipe.reference_values == {"c_one": 0, "c_two": 0, "c_three": 0}
    assert recipe.fit_start_values == {"c_one": 1.5}
    bound = bind_inference_invocation(recipe, fit_config, report_targets=("c_one",))
    assert bound["nuisance_selector"] == "modelconfig_free_non_wc"


def test_nonzero_reference_and_fixed_wc_override(fit_config):
    base = {"schema_version": 1, "family": "impacts_frozen", "dataset_mode": "asimov"}
    benchmark = dict(base, impacts={"reference_values": {"c_two": 2.0}})
    recipe = normalize_inference_recipe(benchmark, fit_config)
    assert recipe.reference_values["c_two"] == 2.0
    for values in ({"extra_one": 1}, {"c_two": 4}):
        with pytest.raises(ValueError):
            normalize_inference_recipe(dict(base, impacts={"reference_values": values}), fit_config)
        unbound = normalize_inference_recipe(dict(base, impacts={"reference_values": values}))
        with pytest.raises(ValueError):
            bind_inference_invocation(unbound, fit_config, report_targets=("c_one",))


def test_recipe_envelopes_exclude_execution_and_release_state(fit_config):
    for forbidden in ("job_set_id", "attempt_id", "result_release", "scan_axes"):
        with pytest.raises(ValueError):
            normalize_inference_recipe({
                "schema_version": 1, "family": "scan_1d_frozen",
                "dataset_mode": "observed", forbidden: "not_recipe_state",
            }, fit_config)


def test_repository_yaml_examples_parse_without_materialization():
    reference_dir = FITTER_DIR.parent / "docs/reference"
    workspace = load_workspace_build_recipe(reference_dir / "workspace_build_recipe_v1.example.yml")
    assert workspace.plugin == "EFTFit.Fitter.EFTModel:eftmodel"
    with (reference_dir / "inference_recipe_v1.examples.yml").open(encoding="utf-8") as stream:
        recipes = [normalize_inference_recipe(data) for data in yaml.safe_load_all(stream)]
    assert {recipe.family for recipe in recipes} == {
        "scan_1d_profiled", "scan_1d_frozen", "scan_2d_profiled", "scan_2d_frozen",
        "impacts_frozen", "impacts_profiled",
    }

"""Synthetic recipe and argv contracts; no Combine or ROOT execution."""

import ast
import hashlib
import importlib.util
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace

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
import inference_recipe as inference_builder  # noqa: E402
from workspace_build_recipe import (  # noqa: E402
    WorkspaceBuildConfig,
    build_workspace_argv,
    load_workspace_build_recipe,
    normalize_workspace_build_recipe,
    run_workspace_build,
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
        "software": {"eftfit_compatibility": "declared_requirement_only"},
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
    monkeypatch.setattr(workspace_builder, "run_workspace_build", lambda config, overwrite, new_version: observed.append((config, overwrite, new_version)))
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


def test_scan_does_not_resolve_impact_reference_vector(monkeypatch):
    config = WCFitConfig("SYNTHETIC", ("c_one", "c_two"), ("extra_one",),
                         {"c_one": (1, 2), "c_two": (3, 4)})
    monkeypatch.setattr(inference_builder, "_bind_reference_values",
                        lambda *args: pytest.fail("scan invoked impact reference validation"))
    for family, axes in (("scan_1d_profiled", ("c_one",)),
                         ("scan_1d_frozen", ("c_one",)),
                         ("scan_2d_profiled", ("c_one", "c_two")),
                         ("scan_2d_frozen", ("c_one", "c_two"))):
        recipe = normalize_inference_recipe(
            {"schema_version": 1, "family": family, "dataset_mode": "observed"}, config)
        assert "reference_values" not in bind_inference_invocation(recipe, config, axes=axes)


@pytest.mark.parametrize("family,axes,targets,allowed,rejected", [
    ("scan_1d_profiled", ("c_one",), (), "c_two", None),
    ("scan_2d_profiled", ("c_one", "c_two"), (), "c_three", None),
    ("scan_1d_frozen", ("c_one",), (), "c_one", "c_two"),
    ("scan_2d_frozen", ("c_one", "c_two"), (), "c_two", "c_three"),
    ("impacts_profiled", (), ("c_one",), "c_two", None),
    ("impacts_frozen", (), ("c_one",), "c_one", "c_two"),
])
def test_family_start_applicability(fit_config, family, axes, targets, allowed, rejected):
    base = {"schema_version": 1, "family": family, "dataset_mode": "observed"}
    recipe = normalize_inference_recipe(dict(base, fit_start_values={allowed: 1.0}), fit_config)
    bound = bind_inference_invocation(recipe, fit_config, axes=axes, report_targets=targets)
    assert bound["fit_start_values"] == {allowed: 1.0}
    if rejected:
        recipe = normalize_inference_recipe(dict(base, fit_start_values={rejected: 1.0}))
        with pytest.raises(ValueError, match="fixed"):
            bind_inference_invocation(recipe, fit_config, axes=axes, report_targets=targets)


def test_impact_start_cannot_override_reference(fit_config):
    recipe = normalize_inference_recipe({
        "schema_version": 1, "family": "impacts_frozen", "dataset_mode": "asimov",
        "impacts": {"reference_values": {"c_one": 0.5}},
        "fit_start_values": {"c_one": -1.0},
    }, fit_config)
    bound = bind_inference_invocation(recipe, fit_config, report_targets=("c_one",))
    assert bound["reference_values"]["c_one"] == 0.5
    assert bound["fit_start_values"]["c_one"] == -1.0


def test_start_values_must_respect_fit_ranges(fit_config):
    data = {"schema_version": 1, "family": "scan_1d_profiled",
            "dataset_mode": "observed", "fit_start_values": {"c_one": 9.0}}
    with pytest.raises(ValueError, match="outside configured range"):
        normalize_inference_recipe(data, fit_config)
    unbound = normalize_inference_recipe(data)
    with pytest.raises(ValueError, match="outside configured range"):
        bind_inference_invocation(unbound, fit_config, axes=("c_one",))


def test_profiled_scan_axis_can_initialize_initial_fit(fit_config):
    recipe = normalize_inference_recipe({
        "schema_version": 1, "family": "scan_1d_profiled", "dataset_mode": "observed",
        "fit_start_values": {"c_one": 1.0},
    }, fit_config)
    bound = bind_inference_invocation(recipe, fit_config, axes=("c_one",))
    assert bound["fit_start_values"] == {"c_one": 1.0}
    assert bound["wc_ranges"] is fit_config.wc_ranges


class SyntheticProcess:
    def __init__(self, argv, content=b"new", returncode=0):
        self.returncode = returncode
        if returncode == 0 and content is not None:
            Path(argv[argv.index("-o") + 1]).write_bytes(content)

    def wait(self):
        return self.returncode

    def communicate(self):
        return b"stdout\n", b"stderr\n"


@pytest.mark.parametrize("initial,policy,expected_name,expected_bytes,raises", [
    (None, "fail", "workspace.root", b"new", False),
    (b"old", "fail", "workspace.root", b"old", True),
    (b"old", "overwrite", "workspace.root", b"new", False),
    (b"old", "new_version", "workspace.v001.root", b"old", False),
])
def test_collision_policies(tmp_path, initial, policy, expected_name, expected_bytes, raises):
    target = tmp_path / "workspace.root"
    if initial is not None:
        target.write_bytes(initial)
    config = WorkspaceBuildConfig("card", "plugin", str(target), existing_policy=policy)
    calls = []

    def factory(argv, **kwargs):
        calls.append(argv)
        return SyntheticProcess(argv)

    if raises:
        with pytest.raises(RuntimeError, match="already exists"):
            run_workspace_build(config, process_factory=factory)
        assert not calls
    else:
        receipt = run_workspace_build(config, process_factory=factory)
        assert Path(receipt["resolved_output_path"]).name == expected_name
        assert receipt["workspace"]["sha256"] == hashlib.sha256(b"new").hexdigest()
        if initial:
            assert receipt["predecessor"]["path"] == str(target)
            assert receipt["predecessor"]["sha256"] == hashlib.sha256(initial).hexdigest()
            assert receipt["predecessor"]["size_bytes"] == len(initial)
        assert (receipt["replaced_artifact"] is not None) == (policy == "overwrite" and initial is not None)
    assert target.read_bytes() == expected_bytes


def test_new_version_chooses_first_free_sibling(tmp_path):
    target = tmp_path / "workspace.root"
    target.write_bytes(b"base")
    (tmp_path / "workspace.v001.root").write_bytes(b"first")
    config = WorkspaceBuildConfig("card", "plugin", str(target), existing_policy="new_version")
    receipt = run_workspace_build(config, process_factory=lambda argv, **kw: SyntheticProcess(argv))
    assert Path(receipt["resolved_output_path"]).name == "workspace.v002.root"
    assert target.read_bytes() == b"base"
    assert (tmp_path / "workspace.v001.root").read_bytes() == b"first"


def test_new_version_without_existing_base_uses_requested_path(tmp_path):
    target = tmp_path / "workspace.root"
    config = WorkspaceBuildConfig("card", "plugin", str(target), existing_policy="new_version")
    receipt = run_workspace_build(config, process_factory=lambda argv, **kw: SyntheticProcess(argv))
    assert receipt["resolved_output_path"] == str(target)
    assert receipt["predecessor"] is None


def test_qualified_receipt_records_observed_commits_without_currentness_gate(tmp_path, monkeypatch):
    card = tmp_path / "card.txt"
    card.write_bytes(b"card")
    target = tmp_path / "workspace.root"
    commits = {"eftfit_commit": "a" * 40, "combined_limit_commit": "b" * 40,
               "cmssw_release": "CMSSW_SYNTHETIC", "scram_arch": "synthetic_arch",
               "text2workspace_executable": "/synthetic/text2workspace.py"}
    monkeypatch.setattr(workspace_builder, "_observed_software", lambda config: commits)
    monkeypatch.setattr(workspace_builder.subprocess, "Popen",
                        lambda argv, **kw: SyntheticProcess(argv))
    config = WorkspaceBuildConfig(str(card), "EFTFit.Fitter.EFTModel:eftmodel", str(target))
    receipt = run_workspace_build(config)
    assert receipt["qualified_provenance"] is True
    assert receipt["observed_software"] == commits
    assert receipt["input"]["combined_card_sha256"] == hashlib.sha256(b"card").hexdigest()
    assert "current_head" not in receipt


def test_dirty_canonical_builder_cannot_claim_clean_commit_provenance(monkeypatch):
    calls = []

    def dirty_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(workspace_builder.subprocess, "run", dirty_run)
    monkeypatch.setenv("CMSSW_BASE", "/synthetic/cmssw")
    monkeypatch.setattr(workspace_builder.shutil, "which", lambda name: "/synthetic/cmssw/src/HiggsAnalysis/CombinedLimit/scripts/text2workspace.py")
    with pytest.raises(RuntimeError, match="not clean"):
        workspace_builder._observed_software(WorkspaceBuildConfig("card", "plugin", "ws"))
    assert calls[0][-1] == "Fitter/python/workspace_build_recipe.py"


def test_observed_software_uses_exact_git_commits(monkeypatch):
    calls = []

    def observed_run(argv, **kwargs):
        calls.append(argv)
        if "diff" in argv:
            return SimpleNamespace(returncode=0)
        commit = "a" * 40 if "EFTFit" in argv[2] else "b" * 40
        return SimpleNamespace(stdout=commit + "\n")

    monkeypatch.setattr(workspace_builder.subprocess, "run", observed_run)
    monkeypatch.setenv("CMSSW_BASE", "/synthetic/cmssw")
    monkeypatch.setattr(workspace_builder.shutil, "which", lambda name: "/synthetic/cmssw/src/HiggsAnalysis/CombinedLimit/scripts/text2workspace.py")
    monkeypatch.setenv("CMSSW_VERSION", "CMSSW_SYNTHETIC")
    monkeypatch.setenv("SCRAM_ARCH", "synthetic_arch")
    observed = workspace_builder._observed_software(
        WorkspaceBuildConfig("card", "EFTFit.Fitter.EFTModel:eftmodel", "ws"))
    assert observed["eftfit_commit"] == "a" * 40
    assert observed["combined_limit_commit"] == "b" * 40
    assert observed["cmssw_release"] == "CMSSW_SYNTHETIC"
    assert any("Fitter/python/EFTModel.py" in argv for argv in calls)
    assert any("scripts/text2workspace.py" in argv for argv in calls)


def test_qualified_provenance_rejects_shadowed_executable(monkeypatch):
    monkeypatch.setenv("CMSSW_BASE", "/synthetic/cmssw")
    monkeypatch.setattr(workspace_builder.shutil, "which", lambda name: "/other/text2workspace.py")
    with pytest.raises(RuntimeError, match="does not resolve"):
        workspace_builder._observed_software(WorkspaceBuildConfig(
            "card", "EFTFit.Fitter.EFTModel:eftmodel", "workspace.root"))


def test_changed_build_source_provenance_blocks_publication(tmp_path, monkeypatch):
    card = tmp_path / "card.txt"
    card.write_bytes(b"card")
    target = tmp_path / "workspace.root"
    states = iter([
        {"eftfit_commit": "a" * 40, "text2workspace_executable": "/synthetic/text2workspace.py"},
        {"eftfit_commit": "b" * 40, "text2workspace_executable": "/synthetic/text2workspace.py"},
    ])
    monkeypatch.setattr(workspace_builder, "_observed_software", lambda config: next(states))
    monkeypatch.setattr(workspace_builder.subprocess, "Popen",
                        lambda argv, **kw: SyntheticProcess(argv))
    config = WorkspaceBuildConfig(str(card), "EFTFit.Fitter.EFTModel:eftmodel", str(target))
    with pytest.raises(RuntimeError, match="changed during execution"):
        run_workspace_build(config)
    assert not target.exists()


def test_failed_overwrite_preserves_predecessor(tmp_path):
    target = tmp_path / "workspace.root"
    target.write_bytes(b"old")
    config = WorkspaceBuildConfig("card", "plugin", str(target), existing_policy="overwrite")
    with pytest.raises(RuntimeError, match="code 7"):
        run_workspace_build(config, process_factory=lambda argv, **kw: SyntheticProcess(argv, returncode=7))
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".workspace.root.*.tmp"))


def test_collision_flags_are_mutually_exclusive(tmp_path):
    config = WorkspaceBuildConfig("card", "plugin", str(tmp_path / "workspace.root"))
    with pytest.raises(ValueError, match="mutually exclusive"):
        run_workspace_build(config, overwrite=True, new_version=True)
    with pytest.raises(SystemExit):
        workspace_builder.main(["--legacy-interference", "--overwrite", "--new-version"])


def test_output_handler_drains_both_large_pipes_with_timeout(tmp_path):
    script = tmp_path / "emit.py"
    script.write_text(
        "import os, sys\n"
        "os.write(2, b'e' * 262144)\n"
        "os.write(1, b'o' * 262144)\n"
        "open(sys.argv[sys.argv.index('-o') + 1], 'wb').write(b'workspace')\n",
        encoding="utf-8",
    )
    config = WorkspaceBuildConfig(str(script), "plugin", str(tmp_path / "workspace.root"),
                                  executable=sys.executable)
    processes, captured, failures = [], [], []

    def factory(argv, **kwargs):
        process = subprocess.Popen(argv, **kwargs)
        processes.append(process)
        return process

    def task():
        try:
            run_workspace_build(config, process_factory=factory,
                                output_handler=lambda stream, level: captured.append((level, stream.read())))
        except Exception as error:
            failures.append(error)

    thread = threading.Thread(target=task, daemon=True)
    thread.start()
    thread.join(timeout=5)
    if thread.is_alive():
        processes[0].kill()
        thread.join(timeout=2)
        pytest.fail("workspace output drain exceeded bounded timeout")
    assert not failures
    assert dict(captured)["info"] == b"o" * 262144
    assert dict(captured)["err"] == b"e" * 262144


def test_default_output_is_inherited(tmp_path, capfd):
    script = tmp_path / "emit.py"
    script.write_text(
        "import os, sys\n"
        "os.write(1, b'operator out\\n')\n"
        "os.write(2, b'operator err\\n')\n"
        "open(sys.argv[sys.argv.index('-o') + 1], 'wb').write(b'workspace')\n",
        encoding="utf-8",
    )
    config = WorkspaceBuildConfig(str(script), "plugin", str(tmp_path / "workspace.root"),
                                  executable=sys.executable)
    run_workspace_build(config, process_factory=subprocess.Popen)
    out, err = capfd.readouterr()
    assert "operator out" in out and "operator err" in err


def test_combine_helper_adapter_executes_canonical_owner(monkeypatch):
    for name in ("CombineHarvester", "CombineHarvester.TopEFT"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    dependencies = {
        "consts": {"EFTFIT_HIST_DIR": "/hist"},
        "CombineHarvester.TopEFT.DatacardMaker": {"DatacardMaker": object},
        "DatacardReader": {"DatacardReader": object},
        "LvlFormatter": {"LvlFormatter": object},
        "utils": {"regex_match": lambda *args: [], "run_command": lambda *args: pytest.fail("second executor"),
                  "CombineMethod": object, "FitAlgo": object,
                  "WorkspaceType": SimpleNamespace(EFT="EFT", SM="SM"), "BatchType": object},
        "options": {"HelperOptions": object},
    }
    for name, values in dependencies.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    path = FITTER_DIR / "python/CombineHelper.py"
    spec = importlib.util.spec_from_file_location("combine_helper_adapter_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = []
    monkeypatch.setattr(module, "run_workspace_build", lambda config: captured.append(config))
    options = {"ws_file": "legacy.root", "datacard_file": "card.txt", "model": "EFTModel:eftmodel",
               "ws_type": "EFT", "phys_ops": ["verbose"], "drop_model_pois": ["c_two"],
               "conversion_file": "fits.npy"}
    fake = module.CombineHelper.__new__(module.CombineHelper)
    fake.logger = logging.getLogger(__name__)
    fake.chdir = lambda path: None
    fake.getOutputDirectory = lambda: "/unused"
    fake.make_parameterization = lambda: None
    fake.ops = SimpleNamespace(getOption=options.__getitem__)
    fake.dc_maker = SimpleNamespace(getOperators=lambda: ["c_one", "c_two"], getBackgroundProcesses=lambda: [])
    fake.make_workspace()
    assert len(captured) == 1
    assert build_workspace_argv(captured[0]) == [
        "text2workspace.py", "card.txt", "-P", "EFTModel:eftmodel",
        "--PO", "verbose", "--PO", "fits=/hist/fits.npy", "--PO", "wcs=c_one",
        "-o", "legacy.root", "--X-allow-no-background",
    ]
    assert captured[0].existing_policy == "fail"

    def rejected(config):
        raise RuntimeError("canonical failure")

    monkeypatch.setattr(module, "run_workspace_build", rejected)
    with pytest.raises(RuntimeError, match="canonical failure"):
        fake.make_workspace()

    captured.clear()
    monkeypatch.setattr(module, "run_workspace_build", lambda config: captured.append(config))
    options.update({
        "ws_type": "SM",
        "model": "HiggsAnalysis.CombinedLimit.PhysicsModel:multiSignalModel",
        "sm_signals": [("ttH", "mu_ttH", [1, 0, 5])],
    })
    fake.setPOIRange = lambda *args: None
    fake.dc_maker.setOperators = lambda operators: None
    fake.make_workspace()
    assert len(captured) == 1
    assert "map=.*/ttH:mu_ttH[1,0,5]" in captured[0].model_options
    assert "wcs=" not in " ".join(captured[0].model_options)

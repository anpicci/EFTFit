import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import types

import pytest


FITTER_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = FITTER_DIR / "scripts" / "EFTFitter.py"


class RecordedProcess:
    def __init__(self, return_code=0):
        self.return_code = return_code
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

    def wait(self):
        return self.return_code


class RecordingSubprocess:
    PIPE = object()

    def __init__(self, aggregation_return_code=0, create_aggregate=False):
        self.aggregation_return_code = aggregation_return_code
        self.create_aggregate = create_aggregate
        self.popen_calls = []
        self.call_calls = []

    def Popen(self, argv, **kwargs):
        argv = list(argv)
        self.popen_calls.append(argv)
        if argv[0] == "combineTool.py":
            task_label = argv[argv.index("--task-name") + 1]
            Path("condor_{}.sh".format(task_label)).write_text(
                "#!/bin/sh\n", encoding="utf-8"
            )
            Path("condor_{}.sub".format(task_label)).write_text(
                "executable = placeholder\nqueue\n", encoding="utf-8"
            )
            return RecordedProcess()
        if argv[0] == "hadd":
            if self.create_aggregate:
                Path(argv[2]).write_bytes(b"aggregate")
            return RecordedProcess(self.aggregation_return_code)
        return RecordedProcess()

    def call(self, argv, **kwargs):
        self.call_calls.append(list(argv))
        return 0


@pytest.fixture
def eftfitter(monkeypatch):
    sys.path.insert(0, str(FITTER_DIR / "python"))
    import wc_fit_config

    root_module = types.ModuleType("ROOT")
    uproot_module = types.ModuleType("uproot")
    eftfit_package = types.ModuleType("EFTFit")
    fitter_package = types.ModuleType("EFTFit.Fitter")
    find_mask_module = types.ModuleType("EFTFit.Fitter.findMask")
    find_mask_module.findMask = lambda value: value
    eftfit_package.__path__ = [str(FITTER_DIR.parent)]
    fitter_package.__path__ = [str(FITTER_DIR / "python")]

    monkeypatch.setitem(sys.modules, "ROOT", root_module)
    monkeypatch.setitem(sys.modules, "uproot", uproot_module)
    monkeypatch.setitem(sys.modules, "EFTFit", eftfit_package)
    monkeypatch.setitem(sys.modules, "EFTFit.Fitter", fitter_package)
    monkeypatch.setitem(sys.modules, "EFTFit.Fitter.findMask", find_mask_module)
    monkeypatch.setitem(
        sys.modules,
        "EFTFit.Fitter.wc_fit_config",
        wc_fit_config,
    )

    spec = importlib.util.spec_from_file_location(
        "eftfitter_execution_provenance_under_test",
        SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_fitter(module):
    fitter = module.EFTFit.__new__(module.EFTFit)
    fitter.log_subprocess_output = lambda pipe, level: None
    return fitter


def make_condor_provenance(module, scan_name=".provenance.c_one"):
    argv = [
        "combineTool.py",
        "-d",
        "/synthetic/workspace.root",
        "-M",
        "MultiDimFit",
        "--algo",
        "grid",
        "--points",
        "20",
        "-n",
        scan_name,
        "-P",
        "c_one",
        "--job-mode",
        "condor",
        "--task-name",
        module._scheduler_task_label(scan_name),
        "--split-points",
        "10",
        "--dry-run",
    ]
    provenance = module._build_condor_provenance(scan_name, argv, 20, 10)
    module._replace_option_value(argv, "-n", provenance["output_label"])
    module._replace_option_value(
        argv,
        "--task-name",
        provenance["scheduler_task_label"],
    )
    provenance["command_argv"] = argv
    module._validate_condor_provenance(provenance, scan_name)
    Path(provenance["execution_directory"]).mkdir()
    module._write_condor_provenance(provenance)
    return provenance


def prepare_retrieval_directory(tmp_path, monkeypatch):
    work_directory = tmp_path / "test"
    fit_directory = tmp_path / "fit_files"
    work_directory.mkdir()
    fit_directory.mkdir()
    monkeypatch.chdir(work_directory)
    return work_directory, fit_directory


def create_expected_outputs(provenance):
    paths = [
        Path(provenance["execution_directory"]) / output
        for output in provenance["expected_outputs"]
    ]
    for path in paths:
        path.write_bytes(b"result")
    return paths


def test_condor_submission_binds_identity_without_changing_semantic_argv(
    eftfitter, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CMSSW_BASE", str(tmp_path / "cmssw"))
    workspace = tmp_path / "workspace.root"
    workspace.write_bytes(b"synthetic")
    recorder = RecordingSubprocess()
    monkeypatch.setattr(eftfitter, "sp", recorder)

    fitter = make_fitter(eftfitter)
    fitter.gridScan(
        name=".provenance.c_one",
        batch="condor",
        freeze=False,
        scan_params=["c_one"],
        params_tracked=["c_two"],
        points=20,
        other=["-t", "-1", "--setParameters", "c_one=0,c_two=0"],
        workspace=str(workspace),
        execution_provenance=True,
    )

    command = next(call for call in recorder.popen_calls if call[0] == "combineTool.py")
    provenance_path = tmp_path / "condor.provenance.c_one" / "execution_provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert eftfitter._validate_condor_provenance(
        provenance, ".provenance.c_one"
    ) == provenance
    assert command[command.index("-n") + 1] == provenance["output_label"]
    assert command[command.index("--task-name") + 1] == provenance[
        "scheduler_task_label"
    ]
    assert eftfitter._semantic_argv(command) == [
        "combineTool.py",
        "-d",
        str(workspace),
        "-M",
        "MultiDimFit",
        "--algo",
        "grid",
        "--cminPreScan",
        "--cminDefaultMinimizerStrategy=0",
        "--points",
        "20",
        "-P",
        "c_one",
        "--trackParameters",
        "c_two",
        "--floatOtherPOIs",
        "1",
        "-t",
        "-1",
        "--setParameters",
        "c_one=0,c_two=0",
        "--job-mode",
        "condor",
        "--split-points",
        "10",
        "--dry-run",
    ]


def test_complete_expected_result_set_succeeds(eftfitter, tmp_path, monkeypatch):
    _, fit_directory = prepare_retrieval_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter)
    expected_paths = create_expected_outputs(provenance)
    unrelated = Path(provenance["execution_directory"]) / "unrelated.root"
    unrelated.write_bytes(b"unrelated")
    for artifact in provenance["submission_artifacts"]:
        Path(artifact).write_text("submission artifact\n", encoding="utf-8")

    recorder = RecordingSubprocess(create_aggregate=True)
    monkeypatch.setattr(eftfitter, "sp", recorder)
    make_fitter(eftfitter).retrieveGridScan(provenance["scan_name"], "condor")

    aggregate = fit_directory / "higgsCombine{}.MultiDimFit.root".format(
        provenance["scan_name"]
    )
    assert aggregate.read_bytes() == b"aggregate"
    assert all(not path.exists() for path in expected_paths)
    assert unrelated.exists()
    assert (
        Path(provenance["execution_directory"]) / "execution_provenance.json"
    ).is_file()
    assert all(
        (Path(provenance["execution_directory"]) / artifact).is_file()
        for artifact in provenance["submission_artifacts"]
    )


def test_non_1d_grid_scan_retains_legacy_identity_path(
    eftfitter, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CMSSW_BASE", str(tmp_path / "cmssw"))
    workspace = tmp_path / "workspace.root"
    workspace.write_bytes(b"synthetic")
    recorder = RecordingSubprocess()
    monkeypatch.setattr(eftfitter, "sp", recorder)

    make_fitter(eftfitter).gridScan(
        name=".legacy.two_dimensional",
        batch="condor",
        scan_params=["c_one", "c_two"],
        points=20,
        workspace=str(workspace),
    )

    command = next(call for call in recorder.popen_calls if call[0] == "combineTool.py")
    assert command[command.index("-n") + 1] == ".legacy.two_dimensional"
    assert command[command.index("--task-name") + 1] == "legacytwo_dimensional"
    assert not (tmp_path / "condor.legacy.two_dimensional" / "execution_provenance.json").exists()


def test_missing_expected_output_fails_closed(eftfitter, tmp_path, monkeypatch):
    prepare_retrieval_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter)
    expected_paths = create_expected_outputs(provenance)
    expected_paths[-1].unlink()
    recorder = RecordingSubprocess()
    monkeypatch.setattr(eftfitter, "sp", recorder)

    with pytest.raises(RuntimeError, match="incomplete or ambiguous"):
        make_fitter(eftfitter).retrieveGridScan(provenance["scan_name"], "condor")

    assert recorder.popen_calls == []
    assert expected_paths[0].is_file()


def test_duplicate_output_identity_fails_closed(eftfitter, tmp_path, monkeypatch):
    prepare_retrieval_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter)
    expected_paths = create_expected_outputs(provenance)
    expected_paths[-1].unlink()
    os.link(expected_paths[0], expected_paths[-1])
    recorder = RecordingSubprocess()
    monkeypatch.setattr(eftfitter, "sp", recorder)

    with pytest.raises(RuntimeError, match="duplicate files"):
        make_fitter(eftfitter).retrieveGridScan(provenance["scan_name"], "condor")

    assert recorder.popen_calls == []
    assert all(path.exists() for path in expected_paths)


def test_stale_identity_metadata_fails_closed(eftfitter, tmp_path, monkeypatch):
    prepare_retrieval_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter)
    create_expected_outputs(provenance)
    provenance_path = (
        Path(provenance["execution_directory"]) / "execution_provenance.json"
    )
    data = json.loads(provenance_path.read_text(encoding="utf-8"))
    data["job_set_id"] = "0" * 64
    provenance_path.write_text(json.dumps(data), encoding="utf-8")
    recorder = RecordingSubprocess()
    monkeypatch.setattr(eftfitter, "sp", recorder)

    with pytest.raises(RuntimeError, match="stale or mismatched"):
        make_fitter(eftfitter).retrieveGridScan(provenance["scan_name"], "condor")

    assert recorder.popen_calls == []


def test_failed_aggregation_preserves_sources(eftfitter, tmp_path, monkeypatch):
    prepare_retrieval_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter)
    expected_paths = create_expected_outputs(provenance)
    recorder = RecordingSubprocess(aggregation_return_code=1)
    monkeypatch.setattr(eftfitter, "sp", recorder)

    with pytest.raises(RuntimeError, match="aggregation failed"):
        make_fitter(eftfitter).retrieveGridScan(provenance["scan_name"], "condor")

    assert all(path.is_file() for path in expected_paths)


def test_crab_resubmission_uses_submission_task_identity(
    eftfitter, monkeypatch
):
    recorder = RecordingSubprocess()
    monkeypatch.setattr(eftfitter, "sp", recorder)
    make_fitter(eftfitter).batchResubmit1DScansEFT(
        basename=".provenance",
        scan_wcs=["c_one"],
    )

    scan_name = eftfitter._one_dimensional_scan_name(".provenance", "c_one")
    expected_project = "crab_{}".format(
        eftfitter._scheduler_task_label(scan_name)
    )
    assert recorder.popen_calls == [["crab", "resubmit", expected_project]]

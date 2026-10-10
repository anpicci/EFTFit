import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from python.execution_provenance import (
    build_direct_attempt_record, build_direct_grid_provenance,
    build_final_provenance, final_provenance_path, resolve_canonical_aggregate,
    sha256_file, validate_direct_attempt_record, validate_direct_grid_provenance,
    workspace_identity, write_json_exclusive,
)


FITTER_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = FITTER_DIR / "scripts" / "EFTFitter.py"


class RecordedProcess:
    def __init__(self, return_code=0):
        self.return_code = return_code
        self.returncode = return_code
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

    def wait(self):
        return self.return_code

    def communicate(self):
        return self.stdout.getvalue(), self.stderr.getvalue()


class RecordingSubprocess:
    PIPE = object()

    def __init__(
        self,
        aggregation_return_code=0,
        aggregate_bytes=b"aggregate",
        workspace_return_code=0,
        workspace_bytes=b"workspace replacement",
    ):
        self.aggregation_return_code = aggregation_return_code
        self.aggregate_bytes = aggregate_bytes
        self.workspace_return_code = workspace_return_code
        self.workspace_bytes = workspace_bytes
        self.popen_calls = []

    def Popen(self, argv, **kwargs):
        argv = list(argv)
        self.popen_calls.append(argv)
        if argv[0] == "combineTool.py":
            task_label = argv[argv.index("--task-name") + 1]
            output_label = argv[argv.index("-n") + 1]
            points = int(argv[argv.index("--points") + 1])
            split_points = int(argv[argv.index("--split-points") + 1])
            dispatcher = ["#!/bin/sh\n", "set -e\n"]
            for member_index, first_point in enumerate(
                range(0, points, split_points)
            ):
                last_point = min(first_point + split_points - 1, points - 1)
                dispatcher.extend(
                    [
                        "if [ $1 -eq {} ]; then\n".format(member_index),
                        "  combine --synthetic-fit --firstPoint {} --lastPoint {} "
                        "-n {}.POINTS.{}.{}\n".format(
                            first_point,
                            last_point,
                            output_label,
                            first_point,
                            last_point,
                        ),
                        "fi\n",
                    ]
                )
            Path("condor_{}.sh".format(task_label)).write_text(
                "".join(dispatcher),
                encoding="utf-8",
            )
            Path("condor_{}.sub".format(task_label)).write_text(
                "executable = placeholder\narguments = $(ProcId)\nqueue 2\n",
                encoding="utf-8",
            )
            return RecordedProcess()
        if argv[0] == "hadd":
            if self.aggregation_return_code == 0 and self.aggregate_bytes is not None:
                Path(argv[2]).write_bytes(self.aggregate_bytes)
            return RecordedProcess(self.aggregation_return_code)
        if argv[0] == "condor_submit":
            return RecordedProcess()
        if "-o" in argv:
            if self.workspace_return_code == 0 and self.workspace_bytes is not None:
                Path(argv[argv.index("-o") + 1]).write_bytes(self.workspace_bytes)
            return RecordedProcess(self.workspace_return_code)
        return RecordedProcess()


@pytest.fixture
def eftfitter(monkeypatch):
    sys.path.insert(0, str(FITTER_DIR / "python"))
    import execution_provenance
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
        "EFTFit.Fitter.execution_provenance",
        execution_provenance,
    )
    monkeypatch.setitem(sys.modules, "EFTFit.Fitter.wc_fit_config", wc_fit_config)

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


def base_condor_argv(workspace, scan_name=".provenance.c_one"):
    return [
        "combineTool.py",
        "-d",
        str(workspace),
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
        scan_name.replace(".", ""),
        "--split-points",
        "10",
        "--dry-run",
    ]


def make_condor_provenance(module, workspace, scan_name=".provenance.c_one"):
    argv = base_condor_argv(workspace, scan_name)
    provenance = module._build_condor_provenance(scan_name, argv, 20, 10)
    module._replace_option_value(argv, "-n", provenance["output_label"])
    module._replace_option_value(
        argv, "--task-name", provenance["scheduler_task_label"]
    )
    provenance["command_argv"] = argv
    module._validate_condor_provenance(provenance, scan_name)
    Path(provenance["attempts_directory"]).mkdir(parents=True)
    initial_directory = Path(provenance["scheduler_directory"]) / "attempt_0001"
    initial_directory.mkdir(parents=True)
    module._write_condor_provenance(provenance)
    shell_path, submit_path = map(Path, provenance["initial_scheduler_artifacts"])
    shell_path.write_text(
        "#!/bin/sh\nset -e\n"
        "if [ $1 -eq 0 ]; then\n"
        "  combine --synthetic-fit -n {}.POINTS.0.9\n"
        "fi\n"
        "if [ $1 -eq 1 ]; then\n"
        "  combine --synthetic-fit -n {}.POINTS.10.19\n"
        "fi\n".format(provenance["output_label"], provenance["output_label"]),
        encoding="utf-8",
    )
    shell_path.chmod(0o755)
    submit_path.write_text(
        "executable = {}\narguments = $(ProcId)\nrequestMemory=10000\n"
        "+JobFlavour = \"workday\"\nqueue 2\n".format(shell_path.resolve()),
        encoding="utf-8",
    )
    module._install_workspace_guard(
        shell_path,
        provenance["workspace_identity"],
        provenance["expected_members"],
        provenance["execution_directory"],
    )
    module._write_attempt_record(
        provenance,
        "initial_submission",
        provenance["expected_members"],
        [str(shell_path.resolve()), str(submit_path.resolve())],
        attempt_number=1,
    )
    return provenance


def prepare_directory(tmp_path, monkeypatch):
    work_directory = tmp_path / "test"
    fit_directory = tmp_path / "fit_files"
    work_directory.mkdir()
    fit_directory.mkdir()
    workspace = work_directory / "workspace.root"
    workspace.write_bytes(b"workspace bytes")
    monkeypatch.chdir(work_directory)
    return workspace, fit_directory


def create_expected_outputs(provenance, content=b"result"):
    paths = [
        Path(provenance["execution_directory"]) / member["file_name"]
        for member in provenance["expected_members"]
    ]
    for index, path in enumerate(paths):
        path.write_bytes(content + str(index).encode("ascii"))
    return paths


def test_initial_submission_binds_workspace_bytes_and_records_attempt(
    eftfitter, tmp_path, monkeypatch
):
    workspace, _ = prepare_directory(tmp_path, monkeypatch)
    monkeypatch.setenv("CMSSW_BASE", str(tmp_path / "cmssw"))
    recorder = RecordingSubprocess()
    monkeypatch.setattr(eftfitter, "sp", recorder)

    make_fitter(eftfitter).gridScan(
        name=".provenance.c_one",
        batch="condor",
        freeze=False,
        scan_params=["c_one"],
        params_tracked=["c_two"],
        points=20,
        other=["-t", "-1", "--setParameters", "c_one=0,c_two=0"],
        workspace=str(workspace),
        execution_provenance=True,
        fit_config="legacy",
    )

    command = next(call for call in recorder.popen_calls if call[0] == "combineTool.py")
    directories = list(Path(".").glob("condor.provenance.c_one.job_*"))
    assert len(directories) == 1
    manifest_path = directories[0] / "execution_provenance.json"
    attempt_path = directories[0] / "attempts" / "attempt_0001.json"
    original_manifest_bytes = manifest_path.read_bytes()
    original_attempt_bytes = attempt_path.read_bytes()
    provenance = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )
    assert eftfitter._validate_condor_provenance(provenance, ".provenance.c_one")
    assert eftfitter._load_condor_provenance(".provenance.c_one", provenance["job_set_id"]) == provenance
    assert len(provenance["job_set_id"]) == 64
    assert provenance["workspace_identity"]["sha256"] == eftfitter.sha256_file(workspace)
    assert eftfitter._semantic_argv(command) == provenance["runtime_semantic_argv"]
    assert (
        eftfitter._identity_semantic_argv(provenance["runtime_semantic_argv"])
        == provenance["identity_semantic_argv"]
    )
    attempt = json.loads(
        attempt_path.read_text(encoding="utf-8")
    )
    assert attempt["job_set_id"] == provenance["job_set_id"]
    assert attempt["reason"] == "initial_submission"
    assert attempt["member_set"] == provenance["expected_members"]
    assert manifest_path.read_bytes() == original_manifest_bytes
    assert attempt_path.read_bytes() == original_attempt_bytes


def test_same_path_changed_bytes_changes_job_identity_and_stales_record(
    eftfitter, tmp_path, monkeypatch
):
    workspace, _ = prepare_directory(tmp_path, monkeypatch)
    first = eftfitter._build_condor_provenance(
        ".provenance.c_one", base_condor_argv(workspace), 20, 10
    )
    first_identity = dict(first["workspace_identity"])
    same = eftfitter._build_condor_provenance(
        ".provenance.c_one", base_condor_argv(workspace), 20, 10
    )
    assert same["job_set_id"] == first["job_set_id"]
    workspace.write_bytes(b"changed workspace bytes")
    changed = eftfitter._build_condor_provenance(
        ".provenance.c_one", base_condor_argv(workspace), 20, 10
    )
    assert changed["job_set_id"] != first["job_set_id"]
    import execution_provenance

    with pytest.raises(RuntimeError, match="identity mismatch"):
        execution_provenance.validate_workspace_identity(first_identity)


def test_same_workspace_bytes_at_different_paths_share_job_identity(
    eftfitter, tmp_path, monkeypatch
):
    workspace, _ = prepare_directory(tmp_path, monkeypatch)
    copy = workspace.with_name("workspace-copy.root")
    copy.write_bytes(workspace.read_bytes())
    first = eftfitter._build_condor_provenance(
        ".provenance.c_one", base_condor_argv(workspace), 20, 10
    )
    second = eftfitter._build_condor_provenance(
        ".provenance.c_one", base_condor_argv(copy), 20, 10
    )
    assert first["runtime_semantic_argv"] != second["runtime_semantic_argv"]
    assert first["identity_semantic_argv"] == second["identity_semantic_argv"]
    assert first["job_set_id"] == second["job_set_id"]


@pytest.mark.parametrize("member_index", [0, 1])
@pytest.mark.parametrize("workspace_state", ["correct", "changed", "missing"])
def test_worker_guard_runs_fit_only_for_exact_workspace(
    eftfitter, tmp_path, monkeypatch, workspace_state, member_index
):
    workspace, _ = prepare_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter, workspace)
    shell_path = Path(provenance["initial_scheduler_artifacts"][0])
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    marker = tmp_path / "fit-invoked"
    combine = binary_directory / "combine"
    combine.write_text(
        "#!/bin/sh\n"
        "label=\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = -n ]; then label=$2; shift 2; else shift; fi\n"
        "done\n"
        "printf invoked > \"{}\"\n"
        "printf member > \"higgsCombine${{label}}.MultiDimFit.mH120.root\"\n".format(
            marker
        ),
        encoding="utf-8",
    )
    combine.chmod(0o755)
    if workspace_state == "changed":
        workspace.write_bytes(b"changed")
    elif workspace_state == "missing":
        workspace.unlink()
    environment = os.environ.copy()
    environment["PATH"] = "{}:{}".format(binary_directory, environment["PATH"])
    result = subprocess.run(
        [str(shell_path.resolve()), str(member_index)],
        env=environment,
        check=False,
    )
    assert (result.returncode == 0) == (workspace_state == "correct")
    assert marker.exists() == (workspace_state == "correct")
    destination = (
        Path(provenance["execution_directory"])
        / provenance["expected_members"][member_index]["file_name"]
    )
    assert destination.exists() == (workspace_state == "correct")


def test_worker_member_publication_never_replaces_existing_destination(
    eftfitter, tmp_path, monkeypatch
):
    workspace, _ = prepare_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter, workspace)
    shell_path = Path(provenance["initial_scheduler_artifacts"][0])
    destination = (
        Path(provenance["execution_directory"])
        / provenance["expected_members"][0]["file_name"]
    )
    destination.write_bytes(b"existing valid member")
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    combine = binary_directory / "combine"
    combine.write_text(
        "#!/bin/sh\n"
        "label=\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = -n ]; then label=$2; shift 2; else shift; fi\n"
        "done\n"
        "printf replacement > \"higgsCombine${label}.MultiDimFit.mH120.root\"\n",
        encoding="utf-8",
    )
    combine.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = "{}:{}".format(binary_directory, environment["PATH"])
    result = subprocess.run(
        [str(shell_path.resolve()), "0"],
        env=environment,
        check=False,
    )
    assert result.returncode != 0
    assert destination.read_bytes() == b"existing valid member"


def test_distinct_job_identities_coexist(eftfitter, tmp_path, monkeypatch):
    workspace, _ = prepare_directory(tmp_path, monkeypatch)
    first = make_condor_provenance(eftfitter, workspace)
    workspace.write_bytes(b"different")
    second = make_condor_provenance(eftfitter, workspace)
    assert first["execution_directory"] != second["execution_directory"]
    assert Path(first["execution_directory"]).is_dir()
    assert Path(second["execution_directory"]).is_dir()


def test_recovery_targets_only_unresolved_members_and_preserves_identity(
    eftfitter, tmp_path, monkeypatch
):
    workspace, _ = prepare_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter, workspace)
    completed = Path(provenance["execution_directory"]) / provenance["expected_members"][0][
        "file_name"
    ]
    completed.write_bytes(b"valid result")
    completed_identity = eftfitter.file_identity(completed)
    recorder = RecordingSubprocess()
    monkeypatch.setattr(eftfitter, "sp", recorder)

    attempt = make_fitter(eftfitter).recoverGridScan(
        provenance["scan_name"], provenance["job_set_id"]
    )

    assert attempt["attempt_id"] == "attempt_0002"
    assert attempt["job_set_id"] == provenance["job_set_id"]
    assert attempt["reason"] == "recovery_missing_members"
    assert attempt["member_set"] == [provenance["expected_members"][1]]
    assert eftfitter.file_identity(completed) == completed_identity
    submit = Path(attempt["scheduler_artifacts"][1]).read_text(encoding="utf-8")
    assert "arguments = $(logical_member)" in submit
    assert "queue logical_member from (\n1\n)" in submit
    assert submit.count("requestMemory") == 1
    assert submit.count("+JobFlavour") == 1
    assert sum(call[0] == "condor_submit" for call in recorder.popen_calls) == 1


def test_recovery_keeps_identity_when_workspace_change_is_left_to_worker_guard(
    eftfitter, tmp_path, monkeypatch
):
    workspace, _ = prepare_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter, workspace)
    workspace.write_bytes(b"changed")
    recorder = RecordingSubprocess()
    monkeypatch.setattr(eftfitter, "sp", recorder)
    attempt = make_fitter(eftfitter).recoverGridScan(
        provenance["scan_name"], provenance["job_set_id"]
    )
    assert attempt["job_set_id"] == provenance["job_set_id"]
    assert attempt["attempt_id"] == "attempt_0002"
    assert sum(call[0] == "condor_submit" for call in recorder.popen_calls) == 1


def test_recovery_noop_when_all_members_are_valid(eftfitter, tmp_path, monkeypatch):
    workspace, _ = prepare_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter, workspace)
    create_expected_outputs(provenance)
    recorder = RecordingSubprocess()
    monkeypatch.setattr(eftfitter, "sp", recorder)
    assert make_fitter(eftfitter).recoverGridScan(
        provenance["scan_name"], provenance["job_set_id"]
    ) is None
    assert recorder.popen_calls == []


def test_duplicate_successful_member_identity_fails_closed(
    eftfitter, tmp_path, monkeypatch
):
    workspace, _ = prepare_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter, workspace)
    paths = create_expected_outputs(provenance)
    paths[1].unlink()
    os.link(paths[0], paths[1])
    with pytest.raises(RuntimeError, match="duplicate files"):
        eftfitter._inspect_condor_members(provenance)


def test_direct_grid_009a_attempt_members_and_resolver(eftfitter, tmp_path, monkeypatch):
    workspace, fit_directory = prepare_directory(tmp_path, monkeypatch)
    base = ["combine", "-d", str(workspace), "-M", "MultiDimFit", "--algo", "grid",
            "--points", "5", "--alignEdges", "1"]
    provenance = build_direct_grid_provenance(
        ".direct.c_one", base, workspace_identity(workspace), 5, [3, 1],
        Path.cwd() / "execution")
    assert validate_direct_grid_provenance(provenance) == provenance
    assert [member["first_point"] for member in provenance["expected_members"]] == [1, 3]
    changed_selection = build_direct_grid_provenance(
        provenance["scan_name"], base, workspace_identity(workspace), 5, [1],
        provenance["execution_root"])
    assert changed_selection["job_set_id"] != provenance["job_set_id"]
    with pytest.raises(ValueError, match="duplicate"):
        build_direct_grid_provenance(provenance["scan_name"], base,
            workspace_identity(workspace), 5, [1, 1], provenance["execution_root"])
    with pytest.raises(ValueError, match="unsupported"):
        build_direct_grid_provenance(provenance["scan_name"], base + ["--gridPoints=5"],
            workspace_identity(workspace), 5, [1], provenance["execution_root"])
    manifest_path = Path(eftfitter._write_condor_provenance(provenance))
    original_bytes = manifest_path.read_bytes()
    assert eftfitter._load_condor_provenance(provenance["scan_name"], provenance["job_set_id"],
                                            provenance["execution_root"]) == provenance
    with pytest.raises(RuntimeError, match="not qualified"):
        make_fitter(eftfitter).retrieveGridScan(provenance["scan_name"], "condor",
            require_condor_provenance=True, job_set_id=provenance["job_set_id"],
            execution_root=provenance["execution_root"])
    assert manifest_path.read_bytes() == original_bytes
    attempt = build_direct_attempt_record(provenance, "campaign_" + "a" * 32,
        "synthetic_no_submit", provenance["expected_members"], manifest_path, [])
    attempt_path = Path(provenance["attempts_directory"]) / (attempt["attempt_id"] + ".json")
    write_json_exclusive(attempt_path, attempt)
    assert eftfitter._validate_attempt_record(provenance, attempt_path, sha256_file(manifest_path)) == attempt
    assert len(eftfitter._attempt_history_references(provenance)) == 1
    with pytest.raises(RuntimeError, match="already exists"):
        write_json_exclusive(attempt_path, attempt)
    duplicate = dict(attempt, member_set=[provenance["expected_members"][0]] * 2)
    with pytest.raises(RuntimeError, match="duplicate"):
        validate_direct_attempt_record(provenance, duplicate, sha256_file(manifest_path))
    foreign = dict(attempt, member_set=[dict(provenance["expected_members"][0], first_point=0)])
    with pytest.raises(RuntimeError, match="foreign"):
        validate_direct_attempt_record(provenance, foreign, sha256_file(manifest_path))
    valid, missing = eftfitter._inspect_condor_members(provenance)
    assert valid == [] and missing == provenance["expected_members"]
    paths = [Path(provenance["execution_directory"]) / member["file_name"]
             for member in provenance["expected_members"]]
    paths[0].write_bytes(b"synthetic member one")
    assert len(eftfitter._inspect_condor_members(provenance)[1]) == 1
    unexpected = paths[0].with_name(paths[0].name.replace(".POINTS.1.1", ".POINTS.2.2"))
    unexpected.write_bytes(b"unexpected")
    with pytest.raises(RuntimeError, match="unexpected"):
        eftfitter._inspect_condor_members(provenance)
    unexpected.unlink()
    os.link(paths[0], paths[1])
    with pytest.raises(RuntimeError, match="duplicate files"):
        eftfitter._inspect_condor_members(provenance)
    paths[1].unlink()
    paths[1].write_bytes(b"synthetic member two")
    assert eftfitter._inspect_condor_members(provenance)[1] == []
    aggregate = Path(provenance["aggregate_output"])
    aggregate.parent.mkdir(parents=True)
    aggregate.write_bytes(b"synthetic aggregate; no fit")
    final = build_final_provenance(provenance, aggregate,
                                   eftfitter._attempt_history_references(provenance),
                                   "synthetic_only")
    write_json_exclusive(final_provenance_path(aggregate), final)
    assert resolve_canonical_aggregate(provenance["scan_name"], provenance["job_set_id"],
                                       aggregate.parent) == str(aggregate.resolve())
    assert fit_directory.exists()


def test_retrieval_requires_complete_members(eftfitter, tmp_path, monkeypatch):
    workspace, _ = prepare_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter, workspace)
    paths = create_expected_outputs(provenance)
    paths[-1].unlink()
    recorder = RecordingSubprocess()
    monkeypatch.setattr(eftfitter, "sp", recorder)
    with pytest.raises(RuntimeError, match="membership is incomplete"):
        make_fitter(eftfitter).retrieveGridScan(
            provenance["scan_name"],
            "condor",
            require_condor_provenance=True,
            job_set_id=provenance["job_set_id"],
        )
    assert recorder.popen_calls == []
    assert paths[0].is_file()


def test_retrieval_publishes_byte_bound_canonical_and_preserves_members(
    eftfitter, tmp_path, monkeypatch
):
    workspace, fit_directory = prepare_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter, workspace)
    members = create_expected_outputs(provenance)
    recorder = RecordingSubprocess(aggregate_bytes=b"canonical aggregate")
    monkeypatch.setattr(eftfitter, "sp", recorder)
    canonical = make_fitter(eftfitter).retrieveGridScan(
        provenance["scan_name"],
        "condor",
        require_condor_provenance=True,
        job_set_id=provenance["job_set_id"],
    )
    canonical = Path(canonical)
    assert canonical.parent == fit_directory.resolve()
    assert provenance["job_set_id"] in canonical.name
    assert canonical.read_bytes() == b"canonical aggregate"
    assert all(path.is_file() for path in members)
    final = json.loads(
        Path(str(canonical) + ".provenance.json").read_text(encoding="utf-8")
    )
    assert final["job_set_id"] == provenance["job_set_id"]
    assert final["canonical_aggregate_sha256"] == eftfitter.sha256_file(canonical)
    assert final["canonical_aggregate_size"] == canonical.stat().st_size
    assert [item["attempt_id"] for item in final["attempt_history_references"]] == [
        "attempt_0001"
    ]


def test_same_identity_equal_candidate_is_noop_and_different_candidate_fails(
    eftfitter, tmp_path, monkeypatch
):
    workspace, _ = prepare_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter, workspace)
    create_expected_outputs(provenance)
    first_recorder = RecordingSubprocess(aggregate_bytes=b"same")
    monkeypatch.setattr(eftfitter, "sp", first_recorder)
    fitter = make_fitter(eftfitter)
    canonical = Path(
        fitter.retrieveGridScan(
            provenance["scan_name"],
            "condor",
            require_condor_provenance=True,
            job_set_id=provenance["job_set_id"],
        )
    )
    first_identity = eftfitter.file_identity(canonical)
    monkeypatch.setattr(eftfitter, "sp", RecordingSubprocess(aggregate_bytes=b"same"))
    assert Path(
        fitter.retrieveGridScan(
            provenance["scan_name"],
            "condor",
            require_condor_provenance=True,
            job_set_id=provenance["job_set_id"],
        )
    ) == canonical
    assert eftfitter.file_identity(canonical) == first_identity
    monkeypatch.setattr(
        eftfitter, "sp", RecordingSubprocess(aggregate_bytes=b"different")
    )
    with pytest.raises(RuntimeError, match="different bytes"):
        fitter.retrieveGridScan(
            provenance["scan_name"],
            "condor",
            require_condor_provenance=True,
            job_set_id=provenance["job_set_id"],
        )
    assert eftfitter.file_identity(canonical) == first_identity


def test_failed_aggregation_preserves_members(eftfitter, tmp_path, monkeypatch):
    workspace, _ = prepare_directory(tmp_path, monkeypatch)
    provenance = make_condor_provenance(eftfitter, workspace)
    members = create_expected_outputs(provenance)
    monkeypatch.setattr(
        eftfitter, "sp", RecordingSubprocess(aggregation_return_code=1)
    )
    with pytest.raises(RuntimeError, match="aggregation failed"):
        make_fitter(eftfitter).retrieveGridScan(
            provenance["scan_name"],
            "condor",
            require_condor_provenance=True,
            job_set_id=provenance["job_set_id"],
        )
    assert all(path.is_file() for path in members)


@pytest.mark.parametrize(
    "initial,overwrite,return_code,replacement,expected,raises",
    [
        (None, False, 0, b"new", b"new", False),
        (b"old", False, 0, b"new", b"old", True),
        (b"old", True, 0, b"new", b"new", False),
        (b"old", True, 7, b"new", b"old", True),
        (b"old", True, 0, b"", b"old", True),
    ],
)
def test_make_workspace_eft_fail_closed_overwrite_lifecycle(
    eftfitter,
    tmp_path,
    monkeypatch,
    initial,
    overwrite,
    return_code,
    replacement,
    expected,
    raises,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CMSSW_BASE", str(tmp_path / "cmssw"))
    Path("card.txt").write_text("card", encoding="utf-8")
    target = Path("workspace.root")
    if initial is not None:
        target.write_bytes(initial)
    recorder = RecordingSubprocess(
        workspace_return_code=return_code,
        workspace_bytes=replacement,
    )
    monkeypatch.setattr(eftfitter, "sp", recorder)
    operation = lambda: make_fitter(eftfitter).makeWorkspaceEFT(
        datacard="card.txt",
        output=str(target),
        overwrite=overwrite,
        text2workspace_executable="fake-text2workspace",
    )
    if raises:
        with pytest.raises(RuntimeError):
            operation()
    else:
        identity = operation()
        assert identity["sha256"] == eftfitter.sha256_file(target)
    assert target.read_bytes() == expected
    assert list(tmp_path.glob(".workspace.root.*.tmp")) == []
    if recorder.popen_calls:
        command = recorder.popen_calls[0]
        output_index = command.index("-o")
        assert command[:output_index] == [
            "fake-text2workspace",
            "card.txt",
            "-P",
            "EFTFit.Fitter.EFTModel:eftmodel",
            "--PO",
            "fits={}/src/EFTFit/Fitter/hist_files/EFT_Parameterization.npy".format(
                tmp_path / "cmssw"
            ),
        ]
        assert command[output_index + 2 :] == ["--channel-masks"]


def test_crab_resubmission_retains_existing_task_identity(eftfitter, monkeypatch):
    recorder = RecordingSubprocess()
    monkeypatch.setattr(eftfitter, "sp", recorder)
    make_fitter(eftfitter).batchResubmit1DScansEFT(
        basename=".provenance", scan_wcs=["c_one"]
    )
    assert recorder.popen_calls == [["crab", "resubmit", "crab_provenancec_one"]]

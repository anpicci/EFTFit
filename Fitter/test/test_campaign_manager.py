"""Local synthetic lifecycle/transaction tests; never execute Combine or submit."""

import importlib.util
import fcntl
import io
import json
import multiprocessing
import os
import queue
import shlex
import stat
import subprocess
from pathlib import Path
import sys
from contextlib import redirect_stdout, redirect_stderr

import numpy as np
import pytest
import uproot
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from python import campaign_manager as manager
from python import campaign_state as state
from python.campaign_backend import condor_plan_backend, local_plan_backend, grid_argv, runtime_identity
from python.campaign_backend import reference_argv
from python.campaign_spec import digest, load_spec
from python.execution_provenance import (compute_job_set_id, file_identity,
    identity_semantic_argv, build_direct_attempt_record, sha256_file, write_json_exclusive,
    validate_direct_grid_provenance, build_direct_grid_provenance, workspace_identity)


def fake_prepare_reference(self, spec, argv, directory, output_path):
    directory.mkdir(parents=True)
    output_path.write_bytes(b"synthetic reference; no Combine execution")
    fit_result = directory / ("multidimfit" + argv[argv.index("-n") + 1] + ".root")
    fit_result.write_bytes(b"synthetic fit result; no Combine execution")
    return {"artifact": file_identity(output_path), "fit_result": file_identity(fit_result),
            "runtime": {"synthetic": True}}


@pytest.mark.parametrize("count,indices,points_per_member,expected", [
    (100, list(range(100)), 5, 20),
    (103, list(range(103)), 5, 21),
    (5, list(range(5)), 5, 1),
    (3, list(range(3)), 5, 1),
    (21, [1, 2, 7, 8, 20], 2, 3),
    (21, [1, 2, 7, 8, 20], "all", 3),
    (100, [i for i in range(100) if i != 2], 5, 21),
    (100, list(range(0, 100, 2)), 5, 50),
    (16, list(range(16)), 5, 4),
])
def test_partition_preserves_effective_original_indices(tmp_path, count, indices,
                                                         points_per_member, expected):
    workspace = tmp_path / "synthetic.root"
    workspace.write_bytes(b"fixture only")
    argv = ["combine", "-d", str(workspace), "-M", "MultiDimFit", "--algo", "grid",
            "--points", str(count), "--alignEdges", "0"]
    manifest = build_direct_grid_provenance(".synthetic", argv, workspace_identity(workspace),
        count, indices, tmp_path / "execution", points_per_member)
    assert validate_direct_grid_provenance(manifest) == manifest
    intervals = manifest["intervals"]
    assert len(intervals) == expected
    flattened = [index for first, last in intervals for index in range(first, last + 1)]
    assert flattened == sorted(indices) and len(flattened) == len(set(flattened))
    assert all(first <= last and (points_per_member == "all" or
               last - first + 1 <= points_per_member) for first, last in intervals)
    assert all(command[command.index("--points") + 1] == str(count)
               for command in manifest["command_argvs"])
    if count == 103:
        assert intervals[-1] == [100, 102]
    if indices == [1, 2, 7, 8, 20]:
        assert intervals == [[1, 2], [7, 8], [20, 20]]
    if count == 100:
        wrong_argv = list(argv)
        wrong_argv[wrong_argv.index("--points") + 1] = "5"
        with pytest.raises(ValueError, match="original point count"):
            build_direct_grid_provenance(".synthetic", wrong_argv,
                workspace_identity(workspace), count, indices,
                tmp_path / "wrong_execution", points_per_member)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "five", None])
def test_invalid_points_per_member_rejected_before_campaign(campaign_inputs, value):
    source, data, root = campaign_inputs
    data["execution"] = {"points_per_member": value}
    source.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="execution.points_per_member"):
        manager.create(root, source)
    assert not root.exists()


def test_new_auto_and_explicit_local_partition(campaign_inputs, monkeypatch):
    source, data, root = campaign_inputs
    data["grid"]["points"] = 103
    source.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(condor_plan_backend, "prepare_reference", fake_prepare_reference)
    created = manager.create(root, source)
    campaign_id = created["campaign_id"]
    with pytest.raises(ValueError, match="prepare-reference"):
        manager.plan(root, campaign_id, backend="condor", dry_run=True)
    manager.prepare_reference(root, campaign_id, "c_one:profiled", backend="condor")
    condor = manager.plan(root, campaign_id, backend="condor", dry_run=True)["plan"]["jobs"][0]
    local = manager.plan(root, campaign_id, backend="local", dry_run=True)["plan"]["jobs"][0]
    assert condor["execution_provenance"]["points_per_member"] == 5
    assert len(condor["expected_members"]) == 21
    assert condor["execution_provenance"]["intervals"][-1] == [100, 102]
    assert local["execution_provenance"]["points_per_member"] == "all"
    assert len(local["expected_members"]) == 1

    data["execution"] = {"points_per_member": 2}
    source.write_text(yaml.safe_dump(data))
    second = manager.create(root, source)["campaign_id"]
    manager.prepare_reference(root, second, "c_one:profiled", backend="local")
    explicit = manager.plan(root, second, backend="local", dry_run=True)["plan"]["jobs"][0]
    assert explicit["execution_provenance"]["points_per_member"] == 2
    assert len(explicit["expected_members"]) == 52


def test_centered_global_grid_survives_five_point_members(campaign_inputs, monkeypatch):
    source, data, root = campaign_inputs
    config_path = source.parent / "package" / "config" / "TOP_26_006.yml"
    config = yaml.safe_load(config_path.read_text())
    config["wc_ranges"]["c_one"] = [-1.6, 1.6]
    config_path.write_text(yaml.safe_dump(config))
    data["package"]["sha256"] = sha256_file(config_path)
    data["grid"] = {"points": 100, "align_edges": False}
    source.write_text(yaml.safe_dump(data))
    spec, _ = load_spec(source)
    points = manager.planned_points(spec)
    assert len(points) == 100
    assert all(point["coordinate"] == pytest.approx(-1.584 + 0.032 * point["point_index"])
               for point in points)
    monkeypatch.setattr(condor_plan_backend, "prepare_reference", fake_prepare_reference)
    campaign_id = manager.create(root, source)["campaign_id"]
    manager.prepare_reference(root, campaign_id, "c_one:profiled", backend="condor")
    planned = manager.plan(root, campaign_id, backend="condor", dry_run=True)
    job = planned["plan"]["jobs"][0]
    assert len(job["expected_members"]) == 20
    assert all(argv[argv.index("--points") + 1] == "100" and
               argv[argv.index("--alignEdges") + 1] == "0"
               for argv in job["command_argvs"])
    wrong = dict(planned["request"]["points"][0], coordinate=0.0)
    with pytest.raises(ValueError, match="original-grid point"):
        condor_plan_backend().plan(dict(planned["request"], points=[wrong]), spec)


def test_reference_intent_success_failure_and_dataset_reuse(campaign_inputs, monkeypatch):
    source, data, root = campaign_inputs
    data["grid"]["points"] = 7
    data["dataset"] = {"mode": "expected", "truth": "sm", "producer_label": "blinded"}
    source.write_text(yaml.safe_dump(data))
    campaign_id = manager.create(root, source)["campaign_id"]
    original = condor_plan_backend.prepare_reference
    def failed(self, spec, argv, directory, output_path):
        raise RuntimeError("synthetic reference failure")
    monkeypatch.setattr(condor_plan_backend, "prepare_reference", failed)
    with pytest.raises(RuntimeError, match="synthetic reference failure"):
        manager.prepare_reference(root, campaign_id, "c_one:profiled", backend="condor")
    assert not manager.status(root, campaign_id).get("references")
    monkeypatch.setattr(condor_plan_backend, "prepare_reference", fake_prepare_reference)
    ready = manager.prepare_reference(root, campaign_id, "c_one:profiled", backend="condor")["reference"]
    assert "--saveFitResult" in ready["argv"]
    assert ready["evidence"]["fit_result"]["sha256"]
    assert ready["argv"] == reference_argv(manager.campaign_store(root, campaign_id).read()["campaign"]["effective_spec"],
                                          manager.status(root, campaign_id)["points"][0],
                                          ".campaign_reference_" + ready["reference_id"])
    planned = manager.plan(root, campaign_id, backend="condor", dry_run=True)
    job = planned["plan"]["jobs"][0]
    assert len(job["expected_members"]) == 2
    assert job["execution_provenance"]["shared_reference"]["artifact"] == ready["artifact"]
    assert all("--toysFile" in argv and "--snapshotName" in argv and
               "--skipInitialFit" in argv and "--setParameters" not in argv and
               argv[argv.index("--points") + 1] == "7" for argv in job["command_argvs"])
    tampered = dict(ready, scientific_contract=dict(ready["scientific_contract"],
                   dataset={"mode": "observed", "name": "data_obs"}))
    with pytest.raises(ValueError, match="scientific contract"):
        condor_plan_backend().plan(dict(planned["request"], references={"c_one:profiled": tampered}),
                                   manager.campaign_store(root, campaign_id).read()["campaign"]["effective_spec"])
    monkeypatch.setattr(condor_plan_backend, "prepare_reference", original)


def test_reference_interruption_is_not_ready(campaign_inputs, monkeypatch):
    source, data, root = campaign_inputs
    data["grid"]["points"] = 6
    source.write_text(yaml.safe_dump(data))
    campaign_id = manager.create(root, source)["campaign_id"]
    def interrupt(self, spec, argv, directory, output_path):
        raise KeyboardInterrupt("synthetic interruption")
    monkeypatch.setattr(condor_plan_backend, "prepare_reference", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare_reference(root, campaign_id, "c_one:profiled", backend="condor")
    assert manager.status(root, campaign_id)["references"]["c_one:profiled"]["state"] == "intent"
    with pytest.raises(ValueError, match="uncertain"):
        manager.plan(root, campaign_id, backend="condor", dry_run=True)


def test_frozen_legacy_spec_uses_maximal_spans(campaign_inputs):
    source, _, root = campaign_inputs
    spec, text = load_spec(source)
    spec.pop("execution")
    spec.pop("reference")
    store = state.campaign_store(root, state.new_id())
    store.create(spec, text, manager.planned_points(spec))
    planned = manager.plan(root, store.campaign_id, backend="condor", dry_run=True)
    manifest = planned["plan"]["jobs"][0]["execution_provenance"]
    assert manifest["schema"] == 4 and len(manifest["expected_members"]) == 1
    assert manifest["expected_members"][0]["first_point"] == 0
    assert manifest["expected_members"][0]["last_point"] == 4

from test_execution_provenance_contract import eftfitter


@pytest.fixture
def campaign_inputs(tmp_path):
    package = tmp_path / "package"
    (package / "config").mkdir(parents=True)
    (package / "datacards").mkdir()
    config = package / "config" / "TOP_26_006.yml"
    config.write_text(yaml.safe_dump({"analysis": "synthetic", "wc_population": {
        "scientific": ["c_one", "c_two"], "fixed_zero": ["c_fixed"]},
        "wc_ranges": {"c_one": [-2, 2], "c_two": [-1, 1]}}))
    workspace = tmp_path / "workspace.root"
    workspace.write_bytes(b"synthetic workspace identity; never executed")
    data = {"schema_version": 1, "package": {"path": str(package), "sha256": sha256_file(config)},
            "workspace": {"path": str(workspace), "sha256": sha256_file(workspace)},
            "dataset": {"mode": "observed", "name": "data_obs"},
            "selection": {"wcs": ["c_one"], "modes": ["profiled"]},
            "grid": {"points": 5, "align_edges": True}, "backend": "provider_a"}
    source = tmp_path / "campaign.yml"
    source.write_text(yaml.safe_dump(data))
    return source, data, tmp_path / "campaigns"


class synthetic_backend(condor_plan_backend):
    """Two provider doubles use genuine 009A serialization, with no execution."""
    capabilities = {"plan": True, "dispatch": True, "collect": True}

    def __init__(self, name, execution_owner):
        self.name = name
        self.execution_owner = execution_owner
        self.cases = {}

    def runtime(self, spec=None):
        return dict(runtime_identity(), provider=self.name, synthetic=True, execution_environment="synthetic")

    def plan(self, request, spec):
        # Retain the accepted schema-3 synthetic receipt fixture unchanged in
        # meaning while shipped providers use the schema-4 direct plan.
        jobs = []
        scans = {}
        for point in request["points"]:
            scans.setdefault(point["scan_id"], []).append(point)
        for scan_points in scans.values():
            groups = []
            for point in scan_points:
                if not groups or point["point_index"] != groups[-1][-1]["point_index"] + 1:
                    groups.append([])
                groups[-1].append(point)
            for group in groups:
                point = group[0]
                scan_name = ".campaign_" + digest({"campaign_id": request["campaign_id"],
                    "generation_id": request["generation_id"], "attempt_id": request["attempt_id"],
                    "scan_id": point["scan_id"], "first_point": point["point_index"]})
                argv = grid_argv(spec, point, request["strategy"])
                argv[0] = "combineTool.py"
                argv += ["--job-mode", "condor", "--split-points", str(spec["grid"]["points"]), "--dry-run"]
                job_set_id = compute_job_set_id("condor", scan_name, identity_semantic_argv(argv),
                                                spec["workspace"]["sha256"])
                argv += ["-n", scan_name + ".job_" + job_set_id,
                         "--task-name", scan_name.replace(".", "") + "_" + job_set_id]
                jobs.append({"job_set_id": job_set_id, "scan_name": scan_name, "argv": argv,
                             "scan_id": point["scan_id"],
                             "point_indices": [p["point_index"] for p in group],
                             "expected_execution_attempt_id": "attempt_0001"})
        return {"requested_backend": request["requested_backend"], "actual_backend": self.name,
                "runtime": self.runtime(), "jobs": jobs, "capabilities": self.capabilities}

    def dispatch(self, attempt, spec, namespace):
        namespace.mkdir(parents=True)
        mapping = []
        previous = Path.cwd()
        try:
            os.chdir(namespace)
            for job in self.plan(attempt, spec)["jobs"]:
                owner = self.execution_owner
                manifest = owner._build_condor_provenance(job["scan_name"], job["argv"], spec["grid"]["points"], spec["grid"]["points"])
                owner._validate_condor_provenance(manifest, job["scan_name"])
                owner._write_condor_provenance(manifest)
                record = owner._write_attempt_record(manifest, "synthetic no-submit", manifest["expected_members"], ["synthetic.sh", "synthetic.sub"], attempt_number=1)
                manifest_path = Path(manifest["execution_directory"]) / "execution_provenance.json"
                record_path = Path(manifest["attempts_directory"]) / (record["attempt_id"] + ".json")
                member = manifest["expected_members"][0]
                path = (Path(manifest["execution_directory"]) / member["file_name"]).resolve()
                selected = [p for p in attempt["points"] if p["scan_id"] == job["scan_id"] and p["point_index"] in job["point_indices"]]
                case = self.cases.get(job["scan_id"], {})
                if case.get("file") == "fake_root":
                    path.write_bytes(b"not ROOT")
                elif case.get("file") != "missing":
                    coordinates, values, quantiles = [], [], []
                    for point in selected:
                        kind = case.get(point["point_index"], "valid")
                        if kind == "partial":
                            continue
                        coordinates.append(point["coordinate"])
                        values.append(9990 if kind in ("failed", "duplicate") else float(point["point_index"]))
                        quantiles.append(-1 if kind == "special_only" else 0)
                        if kind == "duplicate":
                            coordinates.append(point["coordinate"])
                            values.append(0.5)
                            quantiles.append(0)
                    if coordinates:
                        with uproot.recreate(path) as output:
                            tree = output.mktree("limit", {selected[0]["wc"]: "float32", "deltaNLL": "float32", "quantileExpected": "float32"})
                            tree.extend({selected[0]["wc"]: np.array(coordinates, dtype="f4"),
                                         "deltaNLL": np.array(values, dtype="f4"), "quantileExpected": np.array(quantiles, dtype="f4")})
                mapping.append({"job_set_id": job["job_set_id"], "point_indices": job["point_indices"],
                    "manifest": file_identity(manifest_path), "attempt_record": file_identity(record_path),
                    "member": member, "path": str(path)})
            write_json_exclusive(namespace / "mapping.json", mapping)
        finally:
            os.chdir(previous)

    def collect(self, attempt, namespace):
        return json.loads((namespace / "mapping.json").read_text())


@pytest.fixture
def providers(eftfitter, monkeypatch):
    # The existing execution-owner fixture stubs imports for PyROOT isolation.
    # Restore real uproot for independent scientific content validation.
    monkeypatch.setitem(sys.modules, "uproot", uproot)
    return {name: synthetic_backend(name, eftfitter) for name in ("provider_a", "provider_b")}


def snapshot_tree(path):
    if not path.exists():
        return None
    return {str(p.relative_to(path)): ("directory" if p.is_dir() else sha256_file(p)) for p in path.rglob("*")}


def interrupted_restart(root, campaign_id):
    original = state.publish
    def interrupt(path, value):
        if str(path).endswith("event_00000002.commit.json"):
            os._exit(17)
        return original(path, value)
    state.publish = interrupt
    campaign_cli().main(["--root", str(root), "restart", campaign_id])


def campaign_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "eftfit_campaign.py"
    spec = importlib.util.spec_from_file_location("campaign_process_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def concurrent_restart(root, campaign_id, revision, queue):
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = campaign_cli().main(["--root", str(root), "--json", "restart", campaign_id, "--expected-revision", str(revision)])
    queue.put(json.loads(output.getvalue())["active_generation_id"] if code == 0 else json.loads(errors.getvalue())["error"])


def enter_store_lock(root, campaign_id, signals, messages):
    messages.put("ready")
    signals.get(timeout=5)
    messages.put("attempting")
    with state.campaign_store(root, campaign_id).locked():
        messages.put("entered")


def test_store_exclusive_lock_preserves_identity_and_read_only_status(campaign_inputs, monkeypatch):
    source, _, root = campaign_inputs
    created = manager.create(root, source)
    campaign_id = created["campaign_id"]
    store = state.campaign_store(root, campaign_id)
    identity = store.path / "campaign.json"
    before = (identity.read_bytes(), identity.stat().st_ino, stat.S_IMODE(identity.stat().st_mode))
    observed_access = []
    observed_unlocks = []
    original_flock = fcntl.flock

    def observe_flock(descriptor, operation):
        if operation == fcntl.LOCK_EX:
            observed_access.append(fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE)
        elif operation == fcntl.LOCK_UN:
            observed_unlocks.append(descriptor)
        return original_flock(descriptor, operation)

    monkeypatch.setattr(state.fcntl, "flock", observe_flock)
    with store.locked():
        assert store.read()["revision"] == 1
    with pytest.raises(RuntimeError, match="deliberate exit"):
        with store.locked():
            raise RuntimeError("deliberate exit")
    assert observed_access == [os.O_RDWR, os.O_RDWR]
    assert len(observed_unlocks) == 2
    assert (identity.read_bytes(), identity.stat().st_ino, stat.S_IMODE(identity.stat().st_mode)) == before
    assert (store.path / "event_00000001.commit.json").exists()
    assert not store.read()["incomplete"]

    identity.chmod(0o400)
    try:
        assert manager.status(root, campaign_id)["campaign_id"] == campaign_id
        with pytest.raises(PermissionError):
            with store.locked():
                pytest.fail("unwritable identity entered the writer section")
    finally:
        identity.chmod(before[2])
    assert identity.read_bytes() == before[0]
    with pytest.raises(FileExistsError):
        store.create({"conflicting": True}, "different source", [])
    assert (identity.read_bytes(), identity.stat().st_ino, stat.S_IMODE(identity.stat().st_mode)) == before


def test_store_exclusive_lock_serializes_processes_and_releases(campaign_inputs):
    source, _, root = campaign_inputs
    campaign_id = manager.create(root, source)["campaign_id"]
    store = state.campaign_store(root, campaign_id)
    context = multiprocessing.get_context("fork")
    signals = context.Queue()
    messages = context.Queue()
    worker = context.Process(target=enter_store_lock, args=(root, campaign_id, signals, messages))
    try:
        worker.start()
        assert messages.get(timeout=5) == "ready"
        with store.locked():
            signals.put("go")
            assert messages.get(timeout=5) == "attempting"
            with pytest.raises(queue.Empty):
                messages.get(timeout=0.3)
        assert messages.get(timeout=5) == "entered"
        worker.join(5)
        assert worker.exitcode == 0
        with pytest.raises(RuntimeError, match="deliberate exit"):
            with store.locked():
                raise RuntimeError("deliberate exit")
        with store.locked():
            assert store.read()["revision"] == 1
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        signals.close()
        messages.close()


def test_crash_exposed_without_pointer_promotion(campaign_inputs, record_property):
    source, _, root = campaign_inputs
    initial = manager.create(root, source)
    context = multiprocessing.get_context("fork")
    process = context.Process(target=interrupted_restart, args=(root, initial["campaign_id"]))
    process.start()
    process.join(10)
    assert process.exitcode == 17
    before = snapshot_tree(root)
    observed = manager.status(root, initial["campaign_id"])
    assert observed["incomplete"]
    assert observed["active_generation_id"] == initial["active_generation_id"]
    assert snapshot_tree(root) == before
    with pytest.raises(ValueError, match="incomplete"):
        manager.restart(root, initial["campaign_id"])
    record_property("crash_evidence", json.dumps({"child_exitcode": process.exitcode,
        "initial_generation": initial["active_generation_id"], "status": observed,
        "read_only_status": snapshot_tree(root) == before}, sort_keys=True))


def test_simultaneous_restart_cas_and_no_overwrite(campaign_inputs, record_property):
    source, _, root = campaign_inputs
    initial = manager.create(root, source)
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    workers = [context.Process(target=concurrent_restart, args=(root, initial["campaign_id"], 1, queue)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    replies = [queue.get(timeout=2) for _ in workers]
    assert sum("stale revision" in reply for reply in replies) == 1
    observed = manager.status(root, initial["campaign_id"])
    assert len(observed["generations"]) == 2
    assert not observed["incomplete"]
    store = state.campaign_store(root, initial["campaign_id"])
    with pytest.raises(FileExistsError):
        store.create({}, "", [])
    record_property("concurrency_evidence", json.dumps({"replies": replies, "status": observed}, sort_keys=True))


@pytest.mark.parametrize("indices,intervals", [
    ([0, 1, 2, 3, 4], [(0, 4)]), ([0], [(0, 0)]), ([1], [(1, 1)]),
    ([4], [(4, 4)]), ([1, 2, 3], [(1, 3)]), ([1, 3], [(1, 1), (3, 3)]),
])
def test_direct_grid_exact_members_and_queue(campaign_inputs, eftfitter, indices, intervals,
                                             monkeypatch,
                                              record_property):
    source, data, root = campaign_inputs
    data["reference"] = {"mode": "shared_snapshot"}
    source.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(condor_plan_backend, "prepare_reference", fake_prepare_reference)
    created = manager.create(root, source)
    campaign_id = created["campaign_id"]
    manager.prepare_reference(root, campaign_id, "c_one:profiled", backend="condor")
    planned = manager.plan(root, campaign_id, backend="condor", dry_run=True)
    request = planned["request"]
    snapshot = state.campaign_store(root, campaign_id).read()
    spec = snapshot["campaign"]["effective_spec"]
    selected = [point for point in request["points"] if point["point_index"] in indices]
    partial_request = dict(request, points=list(reversed(selected)))
    condor = condor_plan_backend().plan(partial_request, spec)["jobs"][0]
    local = local_plan_backend().plan(partial_request, spec)["jobs"][0]
    manifest = condor["execution_provenance"]
    assert {key: value for key, value in manifest.items() if key != "points_per_member"} == {
        key: value for key, value in local["execution_provenance"].items()
        if key != "points_per_member"}
    assert manifest["schema"] == 5 and manifest["original_points"] == 5
    assert manifest["selected_indices"] == indices
    assert validate_direct_grid_provenance(manifest, condor["scan_name"]) == manifest
    assert [(m["first_point"], m["last_point"]) for m in manifest["expected_members"]] == intervals
    assert len(condor["condor"]["queue"]) == len(intervals)
    assert [item["member_index"] for item in condor["condor"]["queue"]] == list(range(len(intervals)))
    assert len({m["file_name"] for m in manifest["expected_members"]}) == len(intervals)
    assert len(set(condor["working_directories"])) == len(intervals)
    assert "combineTool.py" not in condor["condor"]["worker_script"]
    assert "condor_submit" not in condor["condor"]["submit_description"]
    assert subprocess.run(["/bin/sh", "-n"], input=condor["condor"]["worker_script"],
                          text=True, capture_output=True, check=False).returncode == 0
    rendered_argvs = [shlex.split(line.strip()) for line in condor["condor"]["worker_script"].splitlines()
                     if line.lstrip().startswith("combine ")]
    assert rendered_argvs == condor["command_argvs"]
    queue_rows = condor["condor"]["submit_description"].split("queue member_index, work_directory from (\n", 1)[1].split("\n)\n", 1)[0].splitlines()
    assert queue_rows == ["{} {}".format(item["member_index"], item["work_directory"])
                          for item in condor["condor"]["queue"]]
    for (first, last), argv, member in zip(intervals, condor["command_argvs"], manifest["expected_members"]):
        assert argv[0] == "combine"
        assert argv.count("--points") == argv.count("--firstPoint") == argv.count("--lastPoint") == 1
        assert argv[argv.index("--points") + 1] == "5"
        assert argv[argv.index("--firstPoint") + 1] == str(first)
        assert argv[argv.index("--lastPoint") + 1] == str(last)
        assert member["file_name"] == "higgsCombine{}.MultiDimFit.mH120.root".format(argv[argv.index("-n") + 1])
        assert "--gridPoints" not in argv and "--split-points" not in argv
    assert {index for first, last in intervals for index in range(first, last + 1)} == set(indices)
    record_property("direct_grid_matrix_case", json.dumps({"selected_indices": indices,
        "original_points": 5, "intervals": intervals, "job_set_id": condor["job_set_id"],
        "commands": rendered_argvs, "expected_members": manifest["expected_members"],
        "queue_rows": queue_rows, "local_commands_equal": local["command_argvs"] == rendered_argvs},
        sort_keys=True))


def test_direct_grid_invalid_request_fails_before_attempt(campaign_inputs):
    source, _, root = campaign_inputs
    created = manager.create(root, source)
    planned = manager.plan(root, created["campaign_id"], backend="condor", dry_run=True)
    snapshot = state.campaign_store(root, created["campaign_id"]).read()
    spec = snapshot["campaign"]["effective_spec"]
    request = planned["request"]
    before = snapshot_tree(root)
    for points in ([], request["points"][:1] * 2,
                   [dict(request["points"][1], coordinate=999)],
                   [dict(request["points"][1], point_index=5)]):
        with pytest.raises(ValueError):
            condor_plan_backend().plan(dict(request, points=points), spec)
    assert snapshot_tree(root) == before


def test_direct_grid_scans_provider_generation_and_collision(campaign_inputs):
    source, data, root = campaign_inputs
    data["selection"] = {"wcs": ["c_one", "c_two"], "modes": ["profiled", "frozen"]}
    source.write_text(yaml.safe_dump(data))
    initial = manager.create(root, source)
    campaign_id = initial["campaign_id"]
    condor = manager.plan(root, campaign_id, backend="condor", dry_run=True)
    local = manager.plan(root, campaign_id, backend="local", dry_run=True)
    condor_jobs = condor["plan"]["jobs"]
    local_jobs = local["plan"]["jobs"]
    assert len(condor_jobs) == len(local_jobs) == 4
    assert [(job["scan_id"], job["job_set_id"]) for job in condor_jobs] == [
        (job["scan_id"], job["job_set_id"]) for job in local_jobs]
    assert len({job["job_set_id"] for job in condor_jobs}) == 4
    assert len({member["file_name"] for job in condor_jobs for member in job["expected_members"]}) == 4
    assert len({(job["scan_id"], index) for job in condor_jobs for index in job["point_indices"]}) == 20
    assert manager.plan(root, campaign_id, backend="condor", dry_run=True)["plan"]["jobs"] == condor_jobs
    clone = manager.clone(root, campaign_id)
    assert {job["job_set_id"] for job in manager.plan(root, clone["campaign_id"],
        backend="local", dry_run=True)["plan"]["jobs"]}.isdisjoint(
            job["job_set_id"] for job in condor_jobs)
    manager.restart(root, campaign_id)
    assert {job["job_set_id"] for job in manager.plan(root, campaign_id,
        backend="condor", dry_run=True)["plan"]["jobs"]}.isdisjoint(
            job["job_set_id"] for job in condor_jobs)
    # A collision blocks only its exact attempt root; a fresh attempt is isolated.
    restarted = manager.plan(root, campaign_id, backend="condor", dry_run=True)
    member = restarted["plan"]["jobs"][0]["expected_members"][0]
    directory = Path(restarted["plan"]["jobs"][0]["execution_provenance"]["execution_directory"])
    directory.mkdir(parents=True)
    (directory / member["file_name"]).write_bytes(b"collision")
    before = snapshot_tree(root)
    spec = state.campaign_store(root, campaign_id).read()["campaign"]["effective_spec"]
    with pytest.raises(ValueError, match="already exists"):
        condor_plan_backend().plan(restarted["request"], spec)
    assert snapshot_tree(root) == before
    fresh = manager.plan(root, campaign_id, backend="condor")
    assert fresh["request"]["execution_root"] != restarted["request"]["execution_root"]
    assert fresh["plan"]["jobs"][0]["job_set_id"] == restarted["plan"]["jobs"][0]["job_set_id"]


def test_direct_grid_member_receipt_uses_009a_attempt(campaign_inputs, eftfitter, monkeypatch):
    monkeypatch.setitem(sys.modules, "uproot", uproot)
    source, _, root = campaign_inputs
    created = manager.create(root, source)
    campaign_id = created["campaign_id"]
    planned = manager.plan(root, campaign_id, backend="condor")
    snapshot = state.campaign_store(root, campaign_id).read()
    spec = snapshot["campaign"]["effective_spec"]
    attempt = manager.current_state(snapshot)[2][planned["request"]["attempt_id"]]
    link = attempt["execution_links"][0]
    provenance = planned["plan"]["jobs"][0]["execution_provenance"]
    manifest_path = Path(eftfitter._write_condor_provenance(provenance))
    record = build_direct_attempt_record(provenance, link["expected_execution_attempt_id"],
        "synthetic_receipt", provenance["expected_members"], manifest_path, [])
    record_path = Path(provenance["attempts_directory"]) / (record["attempt_id"] + ".json")
    write_json_exclusive(record_path, record)
    member = provenance["expected_members"][0]
    path = Path(provenance["execution_directory"]) / member["file_name"]
    points = attempt["points"]
    with uproot.recreate(path) as output:
        tree = output.mktree("limit", {"c_one": "float32", "deltaNLL": "float32",
                                         "quantileExpected": "float32"})
        tree.extend({"c_one": np.array([p["coordinate"] for p in points], dtype="f4"),
                     "deltaNLL": np.array(range(5), dtype="f4"),
                     "quantileExpected": np.zeros(5, dtype="f4")})
    mapping = {"manifest": file_identity(manifest_path), "attempt_record": file_identity(record_path),
               "member": member, "point_indices": list(range(5)), "path": str(path)}
    scan, references = manager.validate_member(attempt, link, mapping, spec)
    assert len(scan.grid_points) == 5 and references[-1]["sha256"] == sha256_file(path)
    with pytest.raises(ValueError, match="selected-member linkage"):
        manager.validate_member(attempt, dict(link, point_indices=[1]), mapping, spec)
    with pytest.raises(ValueError, match="incomplete"):
        manager.validate_member(attempt, link, dict(mapping, point_indices=[0]), spec)


def test_condor_path_limit_does_not_poison_local_or_status(campaign_inputs):
    source, _, original_root = campaign_inputs
    root = original_root.with_name("campaign storage")
    created = manager.create(root, source)
    before = snapshot_tree(root)
    with pytest.raises(ValueError, match="Condor submit paths"):
        manager.plan(root, created["campaign_id"], backend="condor")
    assert snapshot_tree(root) == before
    assert manager.plan(root, created["campaign_id"], backend="local", dry_run=True)["plan"]["jobs"]
    assert manager.status(root, created["campaign_id"])["incomplete"] == []
    assert manager.clone(root, created["campaign_id"], dry_run=True)["spec_digest"] == created["spec_digest"]


@pytest.mark.parametrize("change,match", [
    ({"wc_ranges": {"c_one": [-9, 9]}}, "expected fields"),
    ({"fit_start_values": {"c_one": 1}}, "nonzero"),
    ({"keep_failures": True}, "keepFailures"),
    ({"dataset": {"mode": "asimov", "name": "toy"}}, "dataset.mode"),
    ({"selection": {"wcs": [], "modes": ["profiled"]}}, "nonempty"),
])
def test_spec_rejections_are_local(campaign_inputs, change, match):
    source, data, root = campaign_inputs
    initial = manager.create(root, source)
    source.write_text(yaml.safe_dump(dict(data, **change)))
    with pytest.raises(ValueError, match=match):
        load_spec(source)
    assert manager.status(root, initial["campaign_id"])["incomplete"] == []


def test_duplicate_yaml_and_frozen_nonaxis_starts(campaign_inputs):
    source, data, _ = campaign_inputs
    source.write_text(source.read_text() + "schema_version: 1\n")
    with pytest.raises(ValueError, match="unique"):
        load_spec(source)
    data["selection"]["modes"] = ["frozen"]
    data["fit_start_values"] = {"c_two": 0}
    source.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="fixed by this scan"):
        load_spec(source)


@pytest.mark.parametrize("mode,label", [
    ("expected", "blinded"), ("expected", "unblinded"),
    ("observed", "blinded"), ("observed", "unblinded"),
])
def test_fit_dataset_mode_is_independent_of_producer_label(campaign_inputs, mode, label,
                                                           record_property):
    source, data, root = campaign_inputs
    dataset = ({"mode": "expected", "truth": "sm", "producer_label": label}
               if mode == "expected" else
               {"mode": "observed", "name": "data_obs", "producer_label": label})
    data["dataset"] = dataset
    source.write_text(yaml.safe_dump(data))
    created = manager.create(root, source)
    planned = manager.plan(root, created["campaign_id"], backend="local", dry_run=True)
    argv = planned["plan"]["jobs"][0]["command_argvs"][0]
    assert (argv.count("-t") == 1) == (mode == "expected")
    if mode == "expected":
        assert argv[argv.index("-t") + 1] == "-1"
        assert "--dataset" not in argv
    else:
        assert argv[argv.index("--dataset") + 1] == "data_obs"
    assignments = argv[argv.index("--setParameters") + 1].split(",")
    assert assignments == ["c_one=0", "c_two=0", "c_fixed=0"]
    assert "--toysFrequentist" not in argv and "--snapshotName" not in argv
    assert planned["plan"]["jobs"][0]["execution_provenance"]["selected_indices"] == list(range(5))
    record_property("dataset_case", json.dumps({"mode": mode, "producer_label": label,
        "argv": argv, "spec_digest": created["spec_digest"]}, sort_keys=True))


def test_observed_default_name_freezes_without_changing_explicit_legacy_shape(campaign_inputs):
    source, data, _ = campaign_inputs
    explicit, _ = load_spec(source)
    data["dataset"] = {"mode": "observed"}
    source.write_text(yaml.safe_dump(data))
    defaulted, _ = load_spec(source)
    assert defaulted == explicit
    assert defaulted["dataset"] == {"mode": "observed", "name": "data_obs"}


@pytest.mark.parametrize("indices,intervals", [
    ([0, 1, 2, 3, 4], [(0, 4)]), ([0], [(0, 0)]), ([1], [(1, 1)]),
    ([4], [(4, 4)]), ([0, 1], [(0, 1)]), ([3], [(3, 3)]),
    ([1, 2, 3], [(1, 3)]), ([1, 3], [(1, 1), (3, 3)]),
])
def test_yaml_original_index_subset_reaches_both_providers(campaign_inputs, indices,
                                                                  monkeypatch,
                                                              intervals, record_property):
    source, data, root = campaign_inputs
    data["grid"]["selected_indices"] = list(reversed(indices))
    data["reference"] = {"mode": "shared_snapshot"}
    source.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(condor_plan_backend, "prepare_reference", fake_prepare_reference)
    created = manager.create(root, source)
    manager.prepare_reference(root, created["campaign_id"], "c_one:profiled", backend="condor")
    assert len(manager.status(root, created["campaign_id"])["points"]) == 5
    local = manager.plan(root, created["campaign_id"], backend="local", dry_run=True)
    condor = manager.plan(root, created["campaign_id"], backend="condor", dry_run=True)
    assert [p["point_index"] for p in local["request"]["points"]] == indices
    local_manifest = local["plan"]["jobs"][0]["execution_provenance"]
    condor_manifest = condor["plan"]["jobs"][0]["execution_provenance"]
    assert {key: value for key, value in local_manifest.items() if key != "points_per_member"} == {
        key: value for key, value in condor_manifest.items() if key != "points_per_member"}
    job = condor["plan"]["jobs"][0]
    manifest = job["execution_provenance"]
    assert manifest["original_points"] == 5 and manifest["selected_indices"] == indices
    assert [(member["first_point"], member["last_point"])
            for member in manifest["expected_members"]] == intervals
    assert len(job["condor"]["queue"]) == len(intervals)
    assert len(manager.status(root, created["campaign_id"])["attempts"]) == 0
    record_property("subset_case", json.dumps({"indices": indices, "intervals": intervals,
        "job_set_id": job["job_set_id"], "command_argvs": job["command_argvs"],
        "members": manifest["expected_members"], "queue": job["condor"]["queue"]}, sort_keys=True))


@pytest.mark.parametrize("dataset,match", [
    ({"mode": "expected", "truth": "bsm"}, "only sm"),
    ({"mode": "expected", "truth": "sm", "nuisance_generation": "frequentist"}, "unqualified"),
    ({"mode": "expected", "truth": "sm", "snapshot_name": "prefit"}, "expected fields"),
    ({"mode": "expected", "truth": "sm", "bypass_frequentist_fit": True}, "expected fields"),
    ({"mode": "expected"}, "expected fields"),
])
def test_unqualified_expected_policy_fails_before_campaign(campaign_inputs, dataset, match):
    source, data, root = campaign_inputs
    data["dataset"] = dataset
    source.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match=match):
        manager.create(root, source)
    assert not root.exists()


@pytest.mark.parametrize("indices", [[], [0, 0], [5], [-1], [True], [1.0], "1,3"])
def test_invalid_yaml_subset_fails_before_campaign(campaign_inputs, indices):
    source, data, root = campaign_inputs
    data["grid"]["selected_indices"] = indices
    source.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="selected grid"):
        manager.create(root, source)
    assert not root.exists()


def test_grid_coordinates_must_remain_distinguishable(campaign_inputs):
    source, data, root = campaign_inputs
    config = Path(data["package"]["path"]) / "config" / "TOP_26_006.yml"
    package = yaml.safe_load(config.read_text())
    package["wc_ranges"]["c_one"] = [-1e308, 1e308]
    config.write_text(yaml.safe_dump(package))
    data["package"]["sha256"] = sha256_file(config)
    source.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="finite and distinguishable"):
        manager.create(root, source)
    assert not root.exists()

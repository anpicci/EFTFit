"""Local synthetic lifecycle/transaction tests; never execute Combine or submit."""

import importlib.util
import io
import json
import multiprocessing
import os
import shlex
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
from python.campaign_spec import digest, load_spec
from python.execution_provenance import (compute_job_set_id, file_identity,
    identity_semantic_argv, build_direct_attempt_record, sha256_file, write_json_exclusive)

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

    def runtime(self):
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
        code = campaign_cli().main(["--root", str(root), "restart", campaign_id, "--expected-revision", str(revision)])
    queue.put(json.loads(output.getvalue())["active_generation_id"] if code == 0 else json.loads(errors.getvalue())["error"])


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
                                              record_property):
    source, _, root = campaign_inputs
    created = manager.create(root, source)
    campaign_id = created["campaign_id"]
    planned = manager.plan(root, campaign_id, backend="condor", dry_run=True)
    request = planned["request"]
    snapshot = state.campaign_store(root, campaign_id).read()
    spec = snapshot["campaign"]["effective_spec"]
    selected = [point for point in request["points"] if point["point_index"] in indices]
    partial_request = dict(request, points=list(reversed(selected)))
    condor = condor_plan_backend().plan(partial_request, spec)["jobs"][0]
    local = local_plan_backend().plan(partial_request, spec)["jobs"][0]
    manifest = condor["execution_provenance"]
    assert manifest == local["execution_provenance"]
    assert manifest["schema"] == 4 and manifest["original_points"] == 5
    assert manifest["selected_indices"] == indices
    assert eftfitter._validate_condor_provenance(manifest, condor["scan_name"]) == manifest
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
    # A pre-existing identity-owned output must block a new campaign attempt.
    restarted = manager.plan(root, campaign_id, backend="condor", dry_run=True)
    member = restarted["plan"]["jobs"][0]["expected_members"][0]
    directory = Path(restarted["plan"]["jobs"][0]["execution_provenance"]["execution_directory"])
    directory.mkdir(parents=True)
    (directory / member["file_name"]).write_bytes(b"collision")
    before = snapshot_tree(root)
    with pytest.raises(ValueError, match="already exists"):
        manager.plan(root, campaign_id, backend="condor")
    assert snapshot_tree(root) == before


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
    ({"dataset": {"mode": "asimov", "name": "toy"}}, "Asimov"),
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

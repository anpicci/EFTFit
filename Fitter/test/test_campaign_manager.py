"""Local synthetic lifecycle/transaction tests; never execute Combine or submit."""

import importlib.util
import io
import json
import multiprocessing
import os
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
from python.campaign_backend import condor_plan_backend, runtime_identity
from python.campaign_spec import digest, load_spec
from python.execution_provenance import file_identity, sha256_file, write_json_exclusive

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
        result = super().plan(request, spec)
        result["runtime"] = self.runtime()
        return result

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


def test_condor_full_grid_009a_member_and_partial_fail_closed(campaign_inputs, eftfitter):
    source, _, root = campaign_inputs
    created = manager.create(root, source)
    campaign_id = created["campaign_id"]
    planned = manager.plan(root, campaign_id, backend="condor", dry_run=True)
    job = planned["plan"]["jobs"][0]
    assert job["point_indices"] == list(range(5))
    assert job["argv"].count("--points") == 1
    assert job["argv"].count("--split-points") == 1
    assert job["argv"].count("--firstPoint") == job["argv"].count("--lastPoint") == 0
    manifest = eftfitter._build_condor_provenance(job["scan_name"], job["argv"], 5, 5)
    eftfitter._validate_condor_provenance(manifest, job["scan_name"])
    assert manifest["job_set_id"] == job["job_set_id"]
    assert [(m["first_point"], m["last_point"]) for m in manifest["expected_members"]] == [(0, 4)]
    assert len({m["file_name"] for m in manifest["expected_members"]}) == 1
    snapshot = state.campaign_store(root, campaign_id).read()
    spec = snapshot["campaign"]["effective_spec"]
    partial_request = dict(planned["request"], points=planned["request"]["points"][1:2])
    with pytest.raises(ValueError, match="partial-grid planning is unqualified"):
        condor_plan_backend().plan(partial_request, spec)


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

"""Synthetic public Condor bridge tests; scheduler and workers are never launched."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from test_campaign_cli import native_inputs, cli
from test_campaign_manager import campaign_inputs
from python import campaign_condor
from python.campaign_state import campaign_store
from python.execution_provenance import read_json, sha256_file, write_json_exclusive

real_submit_job = campaign_condor.submit_job


@pytest.fixture
def condor_case(native_inputs, monkeypatch, capsys):
    source, root, _, _ = native_inputs
    data = yaml.safe_load(source.read_text())
    data["backend"] = "condor"
    data["dataset"] = {"mode": "expected", "truth": "sm"}
    data["condor_runtime"] = data.pop("local_runtime")
    source.write_text(yaml.safe_dump(data))
    calls = {"submit": [], "query": []}
    def submit_job(path, count):
        calls["submit"].append((str(path), count, path.read_text()))
        return {"argv": ["condor_submit", "-terse", str(path)], "returncode": 0,
                "stdout": "75142.0" if count == 1 else "75142.0 - 75142.{}".format(count - 1),
                "stderr": "", "cluster_id": 75142, "proc_ids": list(range(count))}
    def query_job(cluster, proc):
        calls["query"].append((cluster, proc))
        return {"ClusterId": cluster, "ProcId": proc, "JobStatus": 4, "ExitCode": 0}
    monkeypatch.setattr(campaign_condor, "submit_job", submit_job)
    monkeypatch.setattr(campaign_condor, "query_job", query_job)
    def invoke(*args, expected=0):
        code = cli.main(["--root", str(root), *map(str, args)])
        captured = capsys.readouterr()
        assert code == expected, captured.err
        return json.loads(captured.out if code == 0 else captured.err)
    return source, root, calls, invoke


def prepared(condor_case):
    source, _, calls, invoke = condor_case
    created = invoke("create", source)
    campaign_id = created["campaign_id"]
    preview = invoke("plan", campaign_id, "--backend", "condor", "--dry-run")
    planned = invoke("plan", campaign_id, "--backend", "condor")
    attempt_id = planned["request"]["attempt_id"]
    job = planned["plan"]["jobs"][0]
    invoke("dispatch", campaign_id, attempt_id)
    return campaign_id, attempt_id, job, calls, invoke, preview


def complete_worker(condor_case, job, attempt_id, index=0, status="completed", output="valid"):
    source, _, _, _ = condor_case
    provenance = job["execution_provenance"]
    member = provenance["expected_members"][index]
    work_directory = Path(provenance["scheduler_directory"]) / ("campaign_" + attempt_id) / "member_{:04d}".format(index)
    payload = read_json(work_directory.parent / "worker_payload.json")
    runtime = payload["runtime"]
    outcome = {"attempt_id": "campaign_" + attempt_id, "job_set_id": job["job_set_id"],
        "member": member, "command_argv": job["command_argvs"][index], "runtime": runtime,
        "scheduler_id": [75142, index], "stdout": str(work_directory / "stdout.txt"),
        "stderr": str(work_directory / "stderr.txt"), "finished_utc": "synthetic",
        "exit_code": 0 if status != "nonzero" else 17, "status": status}
    if status == "completed":
        outcome["resolved_runtime"] = {"SCRAM_ARCH": "synthetic_arch",
            "CMSSW_BASE": str(source.parent / "release"),
            "combine_executable": str(source.parent / "release" / "bin" / "synthetic_arch" / "combine"),
            "stack": "unlimited"}
    write_json_exclusive(work_directory / "worker_result.json", outcome)
    if output == "valid":
        os.link(source.parent / "valid.root", Path(provenance["execution_directory"]) / member["file_name"])
    elif output == "foreign":
        (Path(provenance["execution_directory"]) / member["file_name"]).write_bytes(b"foreign ROOT")
    return outcome


def execute_synthetic_worker(condor_case, job, attempt_id, monkeypatch):
    source, _, _, _ = condor_case
    provenance = job["execution_provenance"]
    member = provenance["expected_members"][0]
    work_directory = Path(provenance["scheduler_directory"]) / ("campaign_" + attempt_id) / "member_0000"
    captured = []
    def fake_worker_process(argv, **kwargs):
        captured.append({"argv": argv, "env": kwargs["env"], "cwd": str(kwargs["cwd"])})
        (work_directory / "runtime_resolution.txt").write_text(
            "SCRAM_ARCH=synthetic_arch\nCMSSW_BASE={}\ncombine_executable={}\nstack=unlimited\n".format(
                source.parent / "release", source.parent / "release" / "bin" / "synthetic_arch" / "combine"))
        os.link(source.parent / "valid.root", work_directory / member["file_name"])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(campaign_condor.subprocess, "run", fake_worker_process)
    result = campaign_condor.run_worker(work_directory.parent / "worker_payload.json", "0", "75142", "0")
    return result, captured, work_directory


def test_public_condor_bridge_accepts_valid_member(condor_case, monkeypatch):
    campaign_id, attempt_id, job, calls, invoke, preview = prepared(condor_case)
    assert preview["plan"]["jobs"][0]["command_argvs"] == job["command_argvs"]
    assert job["point_indices"] == [3]
    assert job["expected_members"][0]["first_point"] == job["expected_members"][0]["last_point"] == 3
    assert job["command_argvs"][0][job["command_argvs"][0].index("--points") + 1] == "5"
    assert job["command_argvs"][0][job["command_argvs"][0].index("-n") + 1].endswith("POINTS.3.3")
    assert len(calls["submit"]) == 1 and calls["submit"][0][1] == 1
    assert "arguments = $(member_index) $(ClusterId) $(ProcId)" in calls["submit"][0][2]
    worker_exit, worker_calls, work_directory = execute_synthetic_worker(condor_case, job, attempt_id, monkeypatch)
    assert worker_exit == 0 and len(worker_calls) == 1
    assert worker_calls[0]["argv"][11:] == job["command_argvs"][0]
    assert worker_calls[0]["argv"][1:4] == ["--noprofile", "--norc", "-c"]
    assert "PYTHONPATH" not in worker_calls[0]["env"] and "CONDA_PREFIX" not in worker_calls[0]["env"]
    assert read_json(work_directory / "worker_result.json")["status"] == "completed"
    receipt = invoke("collect", campaign_id, attempt_id)["receipts"][0]
    assert receipt["status"] == "accepted"
    assert receipt["scheduler_id"] == [75142, 0]
    assert calls["query"] == [(75142, 0)]
    assert len(receipt["references"]) == 5
    assert read_json(receipt["references"][1]["resolved_path"])["member_set"] == job["expected_members"]
    assert invoke("status", campaign_id)["points"][3]["status"] == "accepted"
    assert "already dispatched" in invoke("dispatch", campaign_id, attempt_id, expected=2)["error"]
    assert len(calls["submit"]) == 1


def test_worker_collision_preserves_foreign_output(condor_case, monkeypatch):
    campaign_id, attempt_id, job, _, invoke, _ = prepared(condor_case)
    monkeypatch.setattr(campaign_condor, "query_job", lambda cluster, proc: {
        "ClusterId": cluster, "ProcId": proc, "JobStatus": 4, "ExitCode": 1})
    destination = Path(job["execution_provenance"]["execution_directory"]) / job["expected_members"][0]["file_name"]
    destination.write_bytes(b"foreign")
    worker_exit, _, work_directory = execute_synthetic_worker(condor_case, job, attempt_id, monkeypatch)
    assert worker_exit == 1 and destination.read_bytes() == b"foreign"
    assert read_json(work_directory / "worker_result.json")["status"] == "collision"
    receipt = invoke("collect", campaign_id, attempt_id)["receipts"][0]
    assert receipt["status"] == "invalid" and "collision" in receipt["reason"]


def test_worker_publication_failure_keeps_result_unaccepted(condor_case, monkeypatch):
    campaign_id, attempt_id, job, _, invoke, _ = prepared(condor_case)
    monkeypatch.setattr(campaign_condor, "query_job", lambda cluster, proc: {
        "ClusterId": cluster, "ProcId": proc, "JobStatus": 4, "ExitCode": 1})
    destination = Path(job["execution_provenance"]["execution_directory"]) / job["expected_members"][0]["file_name"]
    original_link = os.link
    def fail_publication(source, target, **kwargs):
        if Path(target) == destination:
            raise OSError("synthetic publication failure")
        return original_link(source, target, **kwargs)
    monkeypatch.setattr(os, "link", fail_publication)
    worker_exit, _, work_directory = execute_synthetic_worker(condor_case, job, attempt_id, monkeypatch)
    assert worker_exit == 1 and not destination.exists()
    result = read_json(work_directory / "worker_result.json")
    assert result["status"] == "publication_failed" and "synthetic" in result["error"]
    receipt = invoke("collect", campaign_id, attempt_id)["receipts"][0]
    assert receipt["status"] == "invalid" and "publication_failed" in receipt["reason"]


@pytest.mark.parametrize("state", [1, 2, 5, 6, 7])
def test_live_or_held_condor_is_not_collected(condor_case, monkeypatch, state):
    campaign_id, attempt_id, job, calls, invoke, _ = prepared(condor_case)
    monkeypatch.setattr(campaign_condor, "query_job", lambda cluster, proc: {
        "ClusterId": cluster, "ProcId": proc, "JobStatus": state})
    assert "live, held or uncertain" in invoke("collect", campaign_id, attempt_id, expected=2)["error"]
    assert invoke("status", campaign_id)["points"][3]["status"] == "running"
    assert len(calls["submit"]) == 1


def test_unknown_scheduler_outcome_preserves_dispatch_intent(condor_case, monkeypatch):
    campaign_id, attempt_id, _, calls, invoke, _ = prepared(condor_case)
    def unknown(cluster, proc):
        raise RuntimeError("Condor outcome unknown for synthetic identity")
    monkeypatch.setattr(campaign_condor, "query_job", unknown)
    assert "outcome unknown" in invoke("collect", campaign_id, attempt_id, expected=2)["error"]
    assert invoke("status", campaign_id)["points"][3]["status"] == "running"
    assert len(calls["submit"]) == 1


def test_completed_scheduler_row_without_exit_code_is_uncertain(condor_case, monkeypatch):
    campaign_id, attempt_id, _, _, invoke, _ = prepared(condor_case)
    monkeypatch.setattr(campaign_condor, "query_job", lambda cluster, proc: {
        "ClusterId": cluster, "ProcId": proc, "JobStatus": 4})
    assert "terminal exit is uncertain" in invoke("collect", campaign_id, attempt_id, expected=2)["error"]
    assert invoke("status", campaign_id)["points"][3]["status"] == "running"


def test_missing_worker_or_invalid_root_cannot_be_accepted(condor_case):
    campaign_id, attempt_id, job, _, invoke, _ = prepared(condor_case)
    assert "lacks worker outcome" in invoke("collect", campaign_id, attempt_id, expected=2)["error"]
    complete_worker(condor_case, job, attempt_id, output="foreign")
    receipt = invoke("collect", campaign_id, attempt_id)["receipts"][0]
    assert receipt["status"] == "invalid"


def test_wrong_scheduler_member_is_rejected(condor_case):
    campaign_id, attempt_id, job, _, invoke, _ = prepared(condor_case)
    result = complete_worker(condor_case, job, attempt_id)
    work_directory = Path(job["execution_provenance"]["scheduler_directory"]) / ("campaign_" + attempt_id) / "member_0000"
    # A different scheduler id must not be adopted, even with a valid ROOT.
    path = work_directory / "worker_result.json"
    path.unlink()
    result["scheduler_id"] = [75143, 0]
    write_json_exclusive(path, result)
    assert "outcome identity invalid" in invoke("collect", campaign_id, attempt_id, expected=2)["error"]
    assert invoke("status", campaign_id)["points"][3]["status"] == "running"


def test_missing_submission_response_never_resubmits(condor_case, monkeypatch):
    source, root, _, invoke = condor_case
    monkeypatch.setattr(campaign_condor, "submit_job", real_submit_job)
    launches = []
    class malformed_process:
        returncode = 0
        def communicate(self, timeout):
            return "", ""
    def fake_popen(argv, **kwargs):
        launches.append(argv)
        return malformed_process()
    monkeypatch.setattr(campaign_condor.subprocess, "Popen", fake_popen)
    campaign_id = invoke("create", source)["campaign_id"]
    planned = invoke("plan", campaign_id)
    attempt_id = planned["request"]["attempt_id"]
    job = planned["plan"]["jobs"][0]
    response_path = (Path(job["execution_provenance"]["scheduler_directory"]) /
                     ("campaign_" + attempt_id) / "submission_response.json")
    assert "response is incomplete" in invoke("dispatch", campaign_id, attempt_id, expected=2)["error"]
    snapshot = campaign_store(root, campaign_id).read()
    assert [event["kind"] for event in snapshot["events"]][-1:] == ["dispatch"]
    observed = invoke("status", campaign_id)
    assert observed["attempts"][0]["submission_state"] == "intent"
    assert observed["points"][3]["status"] == "running"
    assert not response_path.exists()
    assert "outcome uncertain" in invoke("collect", campaign_id, attempt_id, expected=2)["error"]
    assert "already dispatched" in invoke("dispatch", campaign_id, attempt_id, expected=2)["error"]
    assert len(launches) == 1


def test_public_proven_no_submit_recovers_same_attempt(condor_case, monkeypatch):
    source, root, _, invoke = condor_case
    monkeypatch.setattr(campaign_condor, "submit_job", real_submit_job)
    starts = []
    launch_allowed = {"value": False}
    class successful_process:
        returncode = 0
        def communicate(self, timeout):
            return "75142.0", ""
    def fake_popen(argv, **kwargs):
        if not launch_allowed["value"]:
            raise PermissionError("synthetic pre-exec denial")
        starts.append(argv)
        return successful_process()
    monkeypatch.setattr(campaign_condor.subprocess, "Popen", fake_popen)
    campaign_id = invoke("create", source)["campaign_id"]
    planned = invoke("plan", campaign_id)
    attempt_id = planned["request"]["attempt_id"]
    job = planned["plan"]["jobs"][0]
    response_path = (Path(job["execution_provenance"]["scheduler_directory"]) /
                     ("campaign_" + attempt_id) / "submission_response.json")
    assert "did not start" in invoke("dispatch", campaign_id, attempt_id, expected=2)["error"]
    before = campaign_store(root, campaign_id).read()
    assert [event["kind"] for event in before["events"]][-2:] == ["dispatch", "submission_not_started"]
    assert before["events"][-1]["payload"] == {"attempt_id": attempt_id, "state": "proven_not_started"}
    assert not response_path.exists() and starts == []
    observed = invoke("status", campaign_id)
    assert observed["attempts"][0]["submission_state"] == "proven_not_started"
    assert observed["points"][3]["status"] == "planned"
    assert "proven not started" in observed["points"][3]["reason"]
    launch_allowed["value"] = True
    invoke("dispatch", campaign_id, attempt_id)
    after = campaign_store(root, campaign_id).read()
    assert after["events"][:len(before["events"])] == before["events"]
    assert after["events"][-1]["kind"] == "dispatch"
    assert len(starts) == 1
    assert read_json(response_path)["cluster_id"] == 75142
    assert read_json(response_path)["proc_ids"] == [0]
    assert read_json(response_path.parent / "submission_intent.json")["member_set"] == job["expected_members"]
    assert "already dispatched" in invoke("dispatch", campaign_id, attempt_id, expected=2)["error"]
    assert len(starts) == 1


def test_later_non_start_cannot_clear_earlier_submitted_scan(condor_case, monkeypatch):
    source, root, _, invoke = condor_case
    config = source.parent / "package" / "config" / "TOP_26_006.yml"
    package = yaml.safe_load(config.read_text())
    package["wc_population"]["scientific"].append("c_two")
    package["wc_ranges"]["c_two"] = [-1, 1]
    config.write_text(yaml.safe_dump(package))
    data = yaml.safe_load(source.read_text())
    data["package"]["sha256"] = sha256_file(config)
    data["selection"]["wcs"].append("c_two")
    source.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(campaign_condor, "submit_job", real_submit_job)
    starts = []
    class successful_process:
        returncode = 0
        def communicate(self, timeout):
            return "75142.0", ""
    def fake_popen(argv, **kwargs):
        if starts:
            raise PermissionError("second scan did not start")
        starts.append(argv)
        return successful_process()
    monkeypatch.setattr(campaign_condor.subprocess, "Popen", fake_popen)
    campaign_id = invoke("create", source)["campaign_id"]
    attempt_id = invoke("plan", campaign_id)["request"]["attempt_id"]
    assert "earlier submitted job" in invoke("dispatch", campaign_id, attempt_id, expected=2)["error"]
    snapshot = campaign_store(root, campaign_id).read()
    assert snapshot["events"][-1]["kind"] == "dispatch"
    assert invoke("status", campaign_id)["attempts"][0]["submission_state"] == "intent"
    assert "already dispatched" in invoke("dispatch", campaign_id, attempt_id, expected=2)["error"]
    assert len(starts) == 1


@pytest.mark.parametrize("stdout, count", [("", 1), ("75142.0", 2),
    ("75142.0 - 75142.2", 2), ("75142.1", 1), ("75142.0 - 75143.1", 2)])
def test_submit_response_rejects_incomplete_or_foreign_range(monkeypatch, tmp_path, stdout, count):
    calls = []
    class fake_process:
        returncode = 0
        def communicate(self, timeout):
            return stdout, ""
    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return fake_process()
    monkeypatch.setattr(campaign_condor.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError, match="response|cardinality"):
        campaign_condor.submit_job(tmp_path / "submit.sub", count)
    assert calls == [["condor_submit", "-terse", str(tmp_path / "submit.sub")]]


@pytest.mark.parametrize("error", [FileNotFoundError("missing executable"),
    PermissionError("permission denied"), OSError("pre-exec failure")])
def test_submit_prelaunch_failure_is_distinct(monkeypatch, tmp_path, error):
    def absent(argv, **kwargs):
        raise error
    monkeypatch.setattr(campaign_condor.subprocess, "Popen", absent)
    with pytest.raises(campaign_condor.submission_not_started, match="did not start"):
        campaign_condor.submit_job(tmp_path / "submit.sub", 1)


def test_submit_started_then_io_error_is_uncertain(monkeypatch, tmp_path):
    class started_process:
        def communicate(self, timeout):
            raise OSError("synthetic lost pipe")
    monkeypatch.setattr(campaign_condor.subprocess, "Popen", lambda *args, **kwargs: started_process())
    with pytest.raises(RuntimeError, match="outcome uncertain after invocation") as observed:
        campaign_condor.submit_job(tmp_path / "submit.sub", 1)
    assert not isinstance(observed.value, campaign_condor.submission_not_started)


def test_exact_job_query_empty_queue_and_history_remains_unknown(monkeypatch):
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")
    monkeypatch.setattr(campaign_condor.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="outcome unknown"):
        campaign_condor.query_job(75142, 0)
    assert calls == [["condor_q", "-json", "75142.0"],
                     ["condor_history", "-json", "75142.0"]]


def test_terminal_scheduler_failure_has_no_success_receipt(condor_case, monkeypatch):
    campaign_id, attempt_id, _, _, invoke, _ = prepared(condor_case)
    monkeypatch.setattr(campaign_condor, "query_job", lambda cluster, proc: {
        "ClusterId": cluster, "ProcId": proc, "JobStatus": 4, "ExitCode": 17})
    receipt = invoke("collect", campaign_id, attempt_id)["receipts"][0]
    assert receipt["status"] == "invalid" and "scheduler_failed" in receipt["reason"]
    assert invoke("status", campaign_id)["points"][3]["status"] == "invalid"


def test_sparse_two_member_submission_uses_planned_queue(condor_case):
    source, _, calls, invoke = condor_case
    data = yaml.safe_load(source.read_text())
    data["grid"]["selected_indices"] = [1, 3]
    source.write_text(yaml.safe_dump(data))
    campaign_id = invoke("create", source)["campaign_id"]
    planned = invoke("plan", campaign_id)
    job = planned["plan"]["jobs"][0]
    assert [(member["first_point"], member["last_point"]) for member in job["expected_members"]] == [(1, 1), (3, 3)]
    invoke("dispatch", campaign_id, planned["request"]["attempt_id"])
    assert len(calls["submit"]) == 1 and calls["submit"][0][1] == 2
    assert "75142.0 - 75142.1" == read_json(Path(calls["submit"][0][0]).parent / "submission_response.json")["stdout"]


def test_sparse_members_keep_independent_terminal_receipts(condor_case, monkeypatch):
    source, _, _, invoke = condor_case
    data = yaml.safe_load(source.read_text())
    data["grid"]["selected_indices"] = [1, 3]
    source.write_text(yaml.safe_dump(data))
    campaign_id = invoke("create", source)["campaign_id"]
    planned = invoke("plan", campaign_id)
    attempt_id = planned["request"]["attempt_id"]
    job = planned["plan"]["jobs"][0]
    invoke("dispatch", campaign_id, attempt_id)
    complete_worker(condor_case, job, attempt_id, index=0, status="nonzero", output="none")
    complete_worker(condor_case, job, attempt_id, index=1, output="valid")
    monkeypatch.setattr(campaign_condor, "query_job", lambda cluster, proc: {
        "ClusterId": cluster, "ProcId": proc, "JobStatus": 4,
        "ExitCode": 17 if proc == 0 else 0})
    receipts = invoke("collect", campaign_id, attempt_id)["receipts"]
    assert [(item["point_index"], item["status"]) for item in receipts] == [(1, "invalid"), (3, "accepted")]
    assert invoke("status", campaign_id)["points"][3]["status"] == "accepted"

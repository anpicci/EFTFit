"""Synthetic public Condor bridge tests; scheduler and workers are never launched."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
import numpy as np
import uproot

from test_campaign_cli import native_inputs, cli
from test_campaign_manager import campaign_inputs, fake_prepare_reference
from python import campaign_condor
from python.campaign_backend import condor_plan_backend, local_plan_backend
from python.campaign_spec import digest, load_spec
from python.campaign_state import campaign_store
from python.execution_provenance import read_json, sha256_file, write_json_exclusive

real_submit_job = campaign_condor.submit_job
real_query_job = campaign_condor.query_job


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
    monkeypatch.setattr(condor_plan_backend, "prepare_reference", fake_prepare_reference)
    def invoke(*args, expected=0):
        code = cli.main(["--root", str(root), "--json", *map(str, args)])
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
    assert calls["submit"][0][2].count("request_memory = 4096M\n") == 1
    assert calls["submit"][0][2].count("request_memory = ") == 1
    assert "request_memory = 10000" not in calls["submit"][0][2]
    assert invoke("status", campaign_id)["attempts"][0]["runtime"]["request_memory_mb"] == 4096
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


@pytest.mark.parametrize("memory", [0, -1, True, "8192", 262145])
def test_condor_memory_schema_rejects_invalid_values(condor_case, memory):
    source, _, _, invoke = condor_case
    data = yaml.safe_load(source.read_text())
    data["condor_runtime"]["request_memory_mb"] = memory
    source.write_text(yaml.safe_dump(data))
    assert "request_memory_mb" in invoke("create", source, expected=2)["error"]


def test_condor_memory_override_and_scientific_identity(condor_case):
    source, _, calls, invoke = condor_case
    default_spec, _ = load_spec(source)
    assert default_spec["condor_runtime"]["request_memory_mb"] == 4096
    legacy_spec = dict(default_spec, condor_runtime={
        key: value for key, value in default_spec["condor_runtime"].items()
        if key != "request_memory_mb"})
    assert condor_plan_backend().runtime(legacy_spec)["request_memory_mb"] == 4096
    local_spec = dict(default_spec, local_runtime=legacy_spec["condor_runtime"])
    assert "request_memory_mb" not in local_plan_backend().runtime(local_spec)
    campaign_id = invoke("create", source)["campaign_id"]
    first = invoke("plan", campaign_id)
    request = first["request"]
    default_job = first["plan"]["jobs"][0]
    data = yaml.safe_load(source.read_text())
    data["condor_runtime"]["request_memory_mb"] = 8192
    source.write_text(yaml.safe_dump(data))
    override_spec, _ = load_spec(source)
    assert override_spec["condor_runtime"]["request_memory_mb"] == 8192
    override_job = condor_plan_backend().plan(request, override_spec)["jobs"][0]
    assert condor_plan_backend().plan(request, legacy_spec)["jobs"][0]["condor"][
        "submit_description"].count("request_memory = 4096M\n") == 1
    assert digest({k: v for k, v in default_spec.items() if k != "condor_runtime"}) == digest(
        {k: v for k, v in override_spec.items() if k != "condor_runtime"})
    for key in ("job_set_id", "scan_name", "point_indices", "expected_members", "command_argvs"):
        assert default_job[key] == override_job[key]
    assert default_job["point_indices"] == [3]
    assert default_job["expected_members"][0]["first_point"] == 3
    assert default_job["expected_members"][0]["last_point"] == 3
    assert default_job["command_argvs"][0][default_job["command_argvs"][0].index("--points") + 1] == "5"
    assert override_job["condor"]["submit_description"].count("request_memory = 8192M\n") == 1
    assert default_job["condor"]["submit_description"].count("request_memory = 4096M\n") == 1
    # A second synthetic campaign proves the override reaches public dispatch.
    override_campaign = invoke("create", source)["campaign_id"]
    override_attempt = invoke("plan", override_campaign)["request"]["attempt_id"]
    invoke("dispatch", override_campaign, override_attempt)
    assert calls["submit"][0][2].count("request_memory = 8192M\n") == 1
    assert calls["submit"][0][2].count("request_memory = ") == 1
    assert invoke("status", override_campaign)["attempts"][0]["runtime"]["request_memory_mb"] == 8192


def test_terminal_removed_condor_retries_same_member_with_default_memory(condor_case, monkeypatch):
    campaign_id, attempt_id, job, calls, invoke, _ = prepared(condor_case)
    old_response = Path(job["execution_provenance"]["scheduler_directory"]) / (
        "campaign_" + attempt_id) / "submission_response.json"
    old_response_bytes = old_response.read_bytes()
    scheduler_calls = []
    def fake_run(argv, **kwargs):
        scheduler_calls.append(argv)
        stdout = "" if argv[0] == "condor_q" else json.dumps([{
            "ClusterId": 75142, "ProcId": 0, "JobStatus": 3,
            "RemoveReason": "via condor_rm (by user apiccine)"}])
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    monkeypatch.setattr(campaign_condor, "query_job", real_query_job)
    monkeypatch.setattr(campaign_condor.subprocess, "run", fake_run)
    receipt = invoke("collect", campaign_id, attempt_id)["receipts"][0]
    assert receipt["status"] == "invalid" and "scheduler_failed" in receipt["reason"]
    assert scheduler_calls == [["condor_q", "-json", "75142.0"],
                               ["condor_history", "-json", "75142.0"]]
    assert invoke("status", campaign_id)["points"][3]["status"] == "invalid"
    retry = invoke("retry", campaign_id, "--backend", "condor", "--scan-id", "ctG:profiled")
    new_attempt = retry["request"]["attempt_id"]
    assert new_attempt != attempt_id
    assert retry["plan"]["jobs"][0]["job_set_id"] == job["job_set_id"]
    assert retry["plan"]["jobs"][0]["point_indices"] == [3]
    assert [(member["first_point"], member["last_point"]) for member in
            retry["plan"]["jobs"][0]["expected_members"]] == [(3, 3)]
    assert retry["plan"]["jobs"][0]["command_argvs"] == job["command_argvs"]
    invoke("dispatch", campaign_id, new_attempt)
    assert len(calls["submit"]) == 2
    assert calls["submit"][1][2].count("request_memory = 4096M\n") == 1
    assert old_response.read_bytes() == old_response_bytes
    assert invoke("status", campaign_id)["attempts"][-1]["runtime"]["request_memory_mb"] == 4096


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


@pytest.mark.parametrize("queue_stdout", ["", " \n\t", "[]"])
def test_exact_job_query_empty_queue_falls_back_to_history(monkeypatch, queue_stdout):
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        stdout = queue_stdout if argv[0] == "condor_q" else json.dumps([{
            "ClusterId": 75142, "ProcId": 0, "JobStatus": 3,
            "RemoveReason": "via condor_rm (by user apiccine)"}])
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    monkeypatch.setattr(campaign_condor.subprocess, "run", fake_run)
    row = campaign_condor.query_job(75142, 0)
    assert row["JobStatus"] == 3 and "condor_rm" in row["RemoveReason"]
    assert calls == [["condor_q", "-json", "75142.0"],
                     ["condor_history", "-json", "75142.0"]]


def test_exact_job_query_active_row_needs_no_history(monkeypatch):
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps([{
            "ClusterId": 75142, "ProcId": 0, "JobStatus": 2}]), stderr="")
    monkeypatch.setattr(campaign_condor.subprocess, "run", fake_run)
    assert campaign_condor.query_job(75142, 0)["JobStatus"] == 2
    assert calls == [["condor_q", "-json", "75142.0"]]


@pytest.mark.parametrize("queue_stdout", ["", "[]"])
@pytest.mark.parametrize("history_stdout", ["", " \n", "[]"])
def test_exact_job_query_absent_from_both_sources_remains_unknown(
        monkeypatch, queue_stdout, history_stdout):
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        stdout = queue_stdout if argv[0] == "condor_q" else history_stdout
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    monkeypatch.setattr(campaign_condor.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="outcome unknown"):
        campaign_condor.query_job(75142, 0)
    assert calls == [["condor_q", "-json", "75142.0"],
                     ["condor_history", "-json", "75142.0"]]


@pytest.mark.parametrize("stdout", ["not-json", "{"])
@pytest.mark.parametrize("malformed_source", ["condor_q", "condor_history"])
def test_exact_job_query_nonempty_malformed_json_fails_closed(
        monkeypatch, stdout, malformed_source):
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        response = stdout if argv[0] == malformed_source else "[]"
        return SimpleNamespace(returncode=0, stdout=response, stderr="")
    monkeypatch.setattr(campaign_condor.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="malformed JSON"):
        campaign_condor.query_job(75142, 0)
    expected = [["condor_q", "-json", "75142.0"]]
    if malformed_source == "condor_history":
        expected.append(["condor_history", "-json", "75142.0"])
    assert calls == expected


def test_exact_job_query_nonzero_exit_does_not_fall_back(monkeypatch):
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="synthetic failure")
    monkeypatch.setattr(campaign_condor.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="condor_q query failed"):
        campaign_condor.query_job(75142, 0)
    assert calls == [["condor_q", "-json", "75142.0"]]


def test_exact_job_query_subprocess_failure_remains_unknown(monkeypatch):
    def fake_run(argv, **kwargs):
        raise OSError("synthetic query launch failure")
    monkeypatch.setattr(campaign_condor.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="outcome unknown"):
        campaign_condor.query_job(75142, 0)


@pytest.mark.parametrize("rows, error", [
    ([{"ClusterId": 75143, "ProcId": 0, "JobStatus": 3}], "identity differs"),
    ([{"ClusterId": 75142, "ProcId": 1, "JobStatus": 3}], "identity differs"),
    ([{"ClusterId": 75142, "ProcId": 0, "JobStatus": 3}] * 2, "cardinality is uncertain")])
def test_exact_job_query_rejects_foreign_or_multiple_rows(monkeypatch, rows, error):
    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
    monkeypatch.setattr(campaign_condor.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match=error):
        campaign_condor.query_job(75142, 0)


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
    invoke("prepare-reference", campaign_id, "--scan-id", "ctG:profiled")
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
    invoke("prepare-reference", campaign_id, "--scan-id", "ctG:profiled")
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
    retry = invoke("retry", campaign_id, "--dry-run")
    assert retry["plan"]["jobs"][0]["point_indices"] == [1]
    assert retry["plan"]["jobs"][0]["execution_provenance"]["shared_reference"] == job["execution_provenance"]["shared_reference"]
    assert retry["plan"]["jobs"][0]["command_argvs"][0][
        retry["plan"]["jobs"][0]["command_argvs"][0].index("--points") + 1] == "5"
    retry = invoke("retry", campaign_id)
    retry_attempt = retry["request"]["attempt_id"]
    retry_job = retry["plan"]["jobs"][0]
    invoke("dispatch", campaign_id, retry_attempt)
    complete_worker(condor_case, retry_job, retry_attempt, output="none")
    retry_member = retry_job["expected_members"][0]
    retry_path = Path(retry_job["execution_provenance"]["execution_directory"]) / retry_member["file_name"]
    coordinate = retry["request"]["points"][0]["coordinate"]
    with uproot.recreate(retry_path) as output:
        tree = output.mktree("limit", {"ctG": "float32", "deltaNLL": "float32",
                                        "quantileExpected": "float32"})
        tree.extend({"ctG": np.array([coordinate], dtype="f4"),
                     "deltaNLL": np.array([1.0], dtype="f4"),
                     "quantileExpected": np.array([0], dtype="f4")})
    monkeypatch.setattr(campaign_condor, "query_job", lambda cluster, proc: {
        "ClusterId": cluster, "ProcId": proc, "JobStatus": 4, "ExitCode": 0})
    recovered = invoke("collect", campaign_id, retry_attempt)["receipts"]
    assert [(item["point_index"], item["status"]) for item in recovered] == [(1, "accepted")]
    assert recovered[0]["reference_group"] == receipts[1]["reference_group"]
    assert invoke("status", campaign_id)["points"][3]["canonical"]["attempt_id"] == attempt_id


def test_shared_reference_collects_distinct_owned_members(condor_case):
    source, _, _, invoke = condor_case
    data = yaml.safe_load(source.read_text())
    data["grid"]["selected_indices"] = [1, 3]
    source.write_text(yaml.safe_dump(data))
    campaign_id = invoke("create", source)["campaign_id"]
    reference = invoke("prepare-reference", campaign_id, "--scan-id", "ctG:profiled")["reference"]
    planned = invoke("plan", campaign_id)
    attempt_id = planned["request"]["attempt_id"]
    job = planned["plan"]["jobs"][0]
    invoke("dispatch", campaign_id, attempt_id)
    for member, point in zip(job["expected_members"], planned["request"]["points"]):
        complete_worker(condor_case, job, attempt_id, index=member["member_index"], output="none")
        output_path = Path(job["execution_provenance"]["execution_directory"]) / member["file_name"]
        with uproot.recreate(output_path) as output:
            tree = output.mktree("limit", {"ctG": "float32", "deltaNLL": "float32",
                                            "quantileExpected": "float32"})
            tree.extend({"ctG": np.array([point["coordinate"]], dtype="f4"),
                         "deltaNLL": np.array([float(point["point_index"])], dtype="f4"),
                         "quantileExpected": np.array([0], dtype="f4")})
    receipts = invoke("collect", campaign_id, attempt_id)["receipts"]
    assert [(item["point_index"], item["status"]) for item in receipts] == [(1, "accepted"), (3, "accepted")]
    assert receipts[0]["reference_group"] == receipts[1]["reference_group"]
    assert receipts[0]["reference_group"]["reference_sha256"] == reference["artifact"]["sha256"]
    assert receipts[0]["execution_link"]["member"] != receipts[1]["execution_link"]["member"]


def test_worker_rejects_changed_shared_reference_before_launch(condor_case, monkeypatch):
    source, _, _, invoke = condor_case
    data = yaml.safe_load(source.read_text())
    data["grid"]["selected_indices"] = [1, 3]
    source.write_text(yaml.safe_dump(data))
    campaign_id = invoke("create", source)["campaign_id"]
    ready = invoke("prepare-reference", campaign_id, "--scan-id", "ctG:profiled")["reference"]
    planned = invoke("plan", campaign_id)
    attempt_id = planned["request"]["attempt_id"]
    job = planned["plan"]["jobs"][0]
    invoke("dispatch", campaign_id, attempt_id)
    reference_path = Path(ready["artifact"]["resolved_path"])
    reference_path.write_bytes(b"changed synthetic reference")
    payload_path = Path(job["execution_provenance"]["scheduler_directory"]) / (
        "campaign_" + attempt_id) / "worker_payload.json"
    def forbidden_run(*args, **kwargs):
        raise AssertionError("worker launched Combine after reference changed")
    monkeypatch.setattr(campaign_condor.subprocess, "run", forbidden_run)
    assert campaign_condor.run_worker(payload_path, "0", "75142", "0") == 1
    result_path = payload_path.parent / "member_0000" / "worker_result.json"
    assert "changed" in read_json(result_path)["error"]

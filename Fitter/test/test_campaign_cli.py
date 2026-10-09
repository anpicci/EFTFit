"""Exercise the maintained CLI parser and lifecycle with real synthetic ROOTs."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from test_campaign_manager import campaign_inputs, providers, eftfitter, snapshot_tree
from python.campaign_backend import condor_plan_backend
from python import campaign_state as state
from python.execution_provenance import sha256_file

cli_path = Path(__file__).resolve().parents[1] / "scripts" / "eftfit_campaign.py"
cli_spec = importlib.util.spec_from_file_location("eftfit_campaign_cli", cli_path)
cli = importlib.util.module_from_spec(cli_spec)
cli_spec.loader.exec_module(cli)


@pytest.fixture
def invoke(capsys, campaign_inputs, providers):
    _, _, root = campaign_inputs
    def run(*args, expected=0, backend_map=providers):
        code = cli.main(["--root", str(root), *map(str, args)], backends=backend_map)
        captured = capsys.readouterr()
        assert code == expected, captured.err
        return json.loads(captured.out if code == 0 else captured.err)
    return run


def run_attempt(invoke, campaign_id, backend, operation="plan", scan_id=None):
    args = [operation, campaign_id, "--backend", backend]
    if scan_id:
        args += ["--scan-id", scan_id]
    if operation == "rescue":
        args += ["--strategy", "minimizer_strategy_1"]
    plan = invoke(*args)
    attempt_id = plan["request"]["attempt_id"]
    invoke("dispatch", campaign_id, attempt_id)
    result = invoke("collect", campaign_id, attempt_id)
    return plan, result


def test_pristine_and_plan_only_recovery_are_no_ops(campaign_inputs, providers, invoke):
    source, _, root = campaign_inputs
    campaign_id = invoke("create", source)["campaign_id"]
    before = snapshot_tree(root)
    assert len(invoke("plan", campaign_id, "--dry-run")["request"]["points"]) == 5
    for operation in ("retry", "rescue"):
        extra = ("--strategy", "minimizer_strategy_1") if operation == "rescue" else ()
        result = invoke(operation, campaign_id, *extra)
        assert result["status"] == "no_eligible_points"
        assert {p["reason"] for p in result["not_yet_recoverable"]} == {"unattempted"}
    assert snapshot_tree(root) == before
    planned = invoke("plan", campaign_id)
    before = snapshot_tree(root)
    assert invoke("retry", campaign_id)["status"] == "no_eligible_points"
    assert {p["reason"] for p in invoke("retry", campaign_id)["not_yet_recoverable"]} == {"not_yet_recoverable_plan_only"}
    assert snapshot_tree(root) == before
    invoke("dispatch", campaign_id, planned["request"]["attempt_id"])
    before = snapshot_tree(root)
    deferred = invoke("retry", campaign_id)
    assert deferred["status"] == "no_eligible_points"
    assert {p["reason"] for p in deferred["not_yet_recoverable"]} == {"not_yet_recoverable_dispatch_intent"}
    assert snapshot_tree(root) == before


def test_six_lifecycle_operations_mixed_backends_and_frozen_spec(campaign_inputs, providers, invoke, record_property):
    source, _, root = campaign_inputs
    initial = invoke("create", source)
    campaign_id = initial["campaign_id"]
    providers["provider_a"].cases["c_one:profiled"] = {1: "failed", 2: "duplicate", 3: "partial"}
    first, _ = run_attempt(invoke, campaign_id, "provider_a")
    observed = invoke("status", campaign_id)
    assert [p["status"] for p in observed["points"]] == ["accepted", "invalid", "accepted", "unresolved", "accepted"]
    assert first["request"]["points"][1]["point_index"] == 1
    before_condor = snapshot_tree(root)
    assert "partial-grid planning is unqualified" in invoke("retry", campaign_id, "--backend", "condor", backend_map={"condor": condor_plan_backend()}, expected=2)["error"]
    assert snapshot_tree(root) == before_condor
    rescue = invoke("rescue", campaign_id, "--backend", "provider_b", "--strategy", "minimizer_strategy_1", "--dry-run")
    assert [p["point_index"] for p in rescue["request"]["points"]] == [1]
    assert rescue["request"]["numerical_difference"] == {"--cminDefaultMinimizerStrategy": {"before": 0, "after": 1}}
    assert "--cminDefaultMinimizerStrategy=1" in rescue["plan"]["jobs"][0]["argv"]
    source.write_text("changed: original YAML no longer controls this campaign\n")
    retry, recovered = run_attempt(invoke, campaign_id, "provider_b", "retry")
    assert [p["point_index"] for p in retry["request"]["points"]] == [1, 3]
    assert {r["reason"] for r in recovered["receipts"]} == {"PROMOTION_UNQUALIFIED_REFERENCE"}
    assert set(j["job_set_id"] for j in first["plan"]["jobs"]).isdisjoint(j["job_set_id"] for j in retry["plan"]["jobs"])
    for job in retry["plan"]["jobs"]:
        index = job["point_indices"][0]
        assert job["argv"][job["argv"].index("--firstPoint") + 1] == str(index)
        assert job["argv"][job["argv"].index("--lastPoint") + 1] == str(index)
    observed = invoke("status", campaign_id)
    assert observed["campaign_id"] == campaign_id
    assert observed["active_generation_id"] == initial["active_generation_id"]
    assert observed["spec_digest"] == initial["spec_digest"]
    assert [p["status"] for p in observed["points"]] == ["accepted", "unresolved", "accepted", "unresolved", "accepted"]
    assert observed["attempts"][-1]["runtime"]["provider"] == "provider_b"
    before = snapshot_tree(root)
    assert invoke("retry", campaign_id, "--backend", "provider_b")["status"] == "no_eligible_points"
    rescue_after = invoke("rescue", campaign_id, "--backend", "provider_b", "--strategy", "minimizer_strategy_1")
    assert rescue_after["status"] == "no_eligible_points"
    assert {p["reason"] for p in rescue_after["not_yet_recoverable"] if p["point_index"] in (1, 3)} == {"promotion_unqualified_reference"}
    assert snapshot_tree(root) == before
    invoke("rescue", campaign_id, "--strategy", "unknown", expected=2)
    cloned = invoke("clone", campaign_id)
    assert cloned["spec_digest"] == initial["spec_digest"]
    assert cloned["campaign_id"] != campaign_id
    assert all(p["status"] == "planned" for p in cloned["points"])
    assert (root / cloned["campaign_id"]) != (root / campaign_id)
    invoke("collect", cloned["campaign_id"], first["request"]["attempt_id"], expected=2)
    restarted = invoke("restart", campaign_id)
    assert restarted["active_generation_id"] != initial["active_generation_id"]
    assert len(restarted["generations"]) == 2
    assert all(p["status"] == "planned" for p in restarted["points"])
    invoke("collect", campaign_id, first["request"]["attempt_id"], expected=2)
    record_property("mixed_provider_evidence", json.dumps({"initial": initial,
        "initial_plan": first, "retry_plan": retry, "recovered": recovered,
        "status_after_retry": observed, "rescue_after_recovery": rescue_after,
        "clone": cloned, "restart": restarted}, sort_keys=True))


def test_parallel_provider_scans_and_unsupported_localization(campaign_inputs, providers, invoke, record_property):
    source, data, root = campaign_inputs
    data["selection"]["wcs"] = ["c_one", "c_two"]
    source.write_text(yaml.safe_dump(data))
    initial = invoke("create", source)
    campaign_id = initial["campaign_id"]
    plan_a = invoke("plan", campaign_id, "--backend", "provider_a", "--scan-id", "c_one:profiled")
    plan_b = invoke("plan", campaign_id, "--backend", "provider_b", "--scan-id", "c_two:profiled")
    for plan in (plan_a, plan_b):
        invoke("dispatch", campaign_id, plan["request"]["attempt_id"])
    assert all(p["status"] == "running" for p in invoke("status", campaign_id)["points"])
    for plan in (plan_b, plan_a):
        invoke("collect", campaign_id, plan["request"]["attempt_id"])
    observed = invoke("status", campaign_id)
    assert all(p["status"] == "accepted" for p in observed["points"])
    del providers["provider_b"]
    before = snapshot_tree(root)
    invoke("retry", campaign_id, "--backend", "provider_b", expected=2)
    assert invoke("status", campaign_id) == observed
    assert invoke("retry", campaign_id, "--backend", "provider_a", "--dry-run")["status"] == "no_eligible_points"
    assert before == snapshot_tree(root)
    record_property("parallel_provider_evidence", json.dumps({"plan_a": plan_a, "plan_b": plan_b,
        "status_after_both": observed, "state_unchanged_after_unavailable_provider": before == snapshot_tree(root)}, sort_keys=True))


@pytest.mark.parametrize("case,expected_status", [("fake_root", "invalid"), ("missing", "missing")])
def test_member_presence_is_not_scientific_acceptance(campaign_inputs, providers, invoke, case, expected_status):
    source, _, _ = campaign_inputs
    initial = invoke("create", source)
    providers["provider_a"].cases["c_one:profiled"] = {"file": case}
    run_attempt(invoke, initial["campaign_id"], "provider_a")
    observed = invoke("status", initial["campaign_id"])
    assert {p["status"] for p in observed["points"]} == {expected_status}
    plan = invoke("retry", initial["campaign_id"], "--backend", "provider_b", "--dry-run")
    assert [p["point_index"] for p in plan["request"]["points"]] == list(range(5))
    assert invoke("rescue", initial["campaign_id"], "--backend", "provider_b", "--strategy", "minimizer_strategy_1")["status"] == "no_eligible_points"


def test_dry_runs_read_only_for_every_mutation_and_status(campaign_inputs, providers, invoke):
    source, _, root = campaign_inputs
    invoke("create", source, "--dry-run")
    assert not root.exists()
    initial = invoke("create", source)
    campaign_id = initial["campaign_id"]
    before = snapshot_tree(root)
    for args in [("clone",), ("restart",), ("plan",), ("retry",), ("rescue", "--strategy", "minimizer_strategy_1")]:
        invoke(args[0], campaign_id, *args[1:], "--dry-run")
        assert snapshot_tree(root) == before
    plan = invoke("plan", campaign_id)
    attempt_id = plan["request"]["attempt_id"]
    before = snapshot_tree(root)
    invoke("dispatch", campaign_id, attempt_id, "--dry-run")
    assert snapshot_tree(root) == before
    invoke("dispatch", campaign_id, attempt_id)
    before = snapshot_tree(root)
    invoke("collect", campaign_id, attempt_id, "--dry-run")
    assert snapshot_tree(root) == before
    invoke("collect", campaign_id, attempt_id)
    manifest = next(root.rglob("execution_provenance.json"))
    manifest.write_text("corrupted")
    before = snapshot_tree(root)
    assert all(p["status"] == "invalid" for p in invoke("status", campaign_id)["points"])
    assert snapshot_tree(root) == before


def test_full_population_condor_planning_and_missing_provider(campaign_inputs, invoke, record_property):
    source, data, root = campaign_inputs
    package_config = Path(data["package"]["path"]) / "config" / "TOP_26_006.yml"
    scientific = ["c_" + str(i) for i in range(26)]
    package_config.write_text(yaml.safe_dump({"analysis": "synthetic_26_10", "wc_population": {
        "scientific": scientific, "fixed_zero": ["fixed_" + str(i) for i in range(10)]},
        "wc_ranges": {wc: [-2, 2] for wc in scientific}}))
    data["package"]["sha256"] = sha256_file(package_config)
    data["selection"] = {"wcs": "all_scientific", "modes": ["profiled", "frozen"]}
    del data["backend"]
    source.write_text(yaml.safe_dump(data))
    initial = invoke("create", source)
    campaign_id = initial["campaign_id"]
    assert len(initial["points"]) == 26 * 2 * 5
    assert "select --backend" in invoke("plan", campaign_id, expected=2, backend_map=None)["error"]
    plan = invoke("plan", campaign_id, "--backend", "condor", "--dry-run", backend_map=None)
    assert len(plan["plan"]["jobs"]) == 52
    assert plan["plan"]["capabilities"] == {"plan": True, "dispatch": False, "collect": False}
    jobs = plan["plan"]["jobs"]
    assert len({job["job_set_id"] for job in jobs}) == len(jobs)
    assert len({job["argv"][job["argv"].index("-n") + 1] for job in jobs}) == len(jobs)
    assert len({(job["scan_id"], index) for job in jobs for index in job["point_indices"]}) == 26 * 2 * 5
    assert all(job["point_indices"] == list(range(5)) for job in jobs)
    job = plan["plan"]["jobs"][0]
    assert "--firstPoint" not in job["argv"] and "--lastPoint" not in job["argv"]
    assert job["point_indices"] == list(range(5))
    actual = invoke("plan", campaign_id, "--backend", "condor", backend_map=None)
    assert "not qualified" in invoke("dispatch", campaign_id, actual["request"]["attempt_id"], backend_map=None, expected=2)["error"]
    assert all(p["status"] == "planned" for p in invoke("status", campaign_id, backend_map={})["points"])
    record_property("full_population_plan", json.dumps({"scan_ids": sorted({p["scan_id"] for p in initial["points"]}),
        "point_count": len(initial["points"]), "job_count": len(plan["plan"]["jobs"]),
        "capabilities": plan["plan"]["capabilities"], "example_job": plan["plan"]["jobs"][0]}, sort_keys=True))


def test_script_entrypoint_through_wrapper(campaign_inputs):
    source, _, root = campaign_inputs
    command = ["/users/apiccine/work/correction-lib/codex-run.sh", sys.executable, str(cli_path), "--root", str(root)]
    result = subprocess.run(command + ["create", str(source)], text=True, capture_output=True, check=True)
    initial = json.loads(result.stdout)
    result = subprocess.run(command + ["status", initial["campaign_id"]], text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == initial


def test_special_entry_is_not_grid_success_and_first_valid_recovery(campaign_inputs, providers, invoke):
    source, _, _ = campaign_inputs
    initial = invoke("create", source)
    campaign_id = initial["campaign_id"]
    providers["provider_a"].cases["c_one:profiled"] = {index: "special_only" for index in range(5)}
    run_attempt(invoke, campaign_id, "provider_a")
    assert all(p["status"] != "accepted" for p in invoke("status", campaign_id)["points"])
    run_attempt(invoke, campaign_id, "provider_b", "retry")
    assert all(p["status"] == "accepted" for p in invoke("status", campaign_id)["points"])


def test_stale_dispatch_and_foreign_member_mapping(campaign_inputs, providers, invoke):
    source, _, root = campaign_inputs
    initial = invoke("create", source)
    campaign_id = initial["campaign_id"]
    stale = invoke("plan", campaign_id, "--backend", "provider_b")
    plan, _ = run_attempt(invoke, campaign_id, "provider_a")
    assert "stale plan" in invoke("dispatch", campaign_id, stale["request"]["attempt_id"], expected=2)["error"]
    restarted = invoke("restart", campaign_id)
    fresh = invoke("plan", campaign_id, "--backend", "provider_a")
    invoke("dispatch", campaign_id, fresh["request"]["attempt_id"])
    old_mapping = root / campaign_id / "execution" / plan["request"]["attempt_id"] / "mapping.json"
    new_mapping = root / campaign_id / "execution" / fresh["request"]["attempt_id"] / "mapping.json"
    new_mapping.write_bytes(old_mapping.read_bytes())
    assert "foreign execution" in invoke("collect", campaign_id, fresh["request"]["attempt_id"], expected=2)["error"]
    assert all(p["canonical"] is None for p in invoke("status", campaign_id)["points"])


def test_identity_owned_content_and_corruption_are_per_scan(campaign_inputs, providers, invoke):
    source, data, root = campaign_inputs
    data["selection"]["wcs"] = ["c_one", "c_two"]
    source.write_text(yaml.safe_dump(data))
    initial = invoke("create", source)
    campaign_id = initial["campaign_id"]
    plan_a, _ = run_attempt(invoke, campaign_id, "provider_a", scan_id="c_one:profiled")
    run_attempt(invoke, campaign_id, "provider_b", scan_id="c_two:profiled")
    path = next((root / campaign_id / "execution" / plan_a["request"]["attempt_id"]).rglob("*.root"))
    path.unlink()
    observed = invoke("status", campaign_id, backend_map={})
    assert {p["status"] for p in observed["points"] if p["wc"] == "c_one"} == {"missing"}
    assert {p["status"] for p in observed["points"] if p["wc"] == "c_two"} == {"accepted"}
    retry = invoke("retry", campaign_id, "--backend", "provider_b", "--dry-run")
    assert retry["status"] == "no_eligible_points"
    assert {p["reason"] for p in retry["not_yet_recoverable"] if p["scan_id"] == "c_one:profiled"} == {"canonical_reference_damaged"}

"""Exercise the maintained CLI parser and lifecycle with real synthetic ROOTs."""

import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys

import numpy as np
import pytest
import uproot
import yaml

from test_campaign_manager import campaign_inputs, providers, eftfitter, snapshot_tree, fake_prepare_reference
from python.campaign_backend import condor_plan_backend
from python import campaign_backend
from python import campaign_state as state
from python.campaign_spec import load_spec, planned_points
from python.execution_provenance import read_json, sha256_file

cli_path = Path(__file__).resolve().parents[1] / "scripts" / "eftfit_campaign.py"
cli_spec = importlib.util.spec_from_file_location("eftfit_campaign_cli", cli_path)
cli = importlib.util.module_from_spec(cli_spec)
cli_spec.loader.exec_module(cli)


@pytest.fixture
def invoke(capsys, campaign_inputs, providers):
    _, _, root = campaign_inputs
    def run(*args, expected=0, backend_map=providers):
        code = cli.main(["--root", str(root), "--json", *map(str, args)], backends=backend_map)
        captured = capsys.readouterr()
        assert code == expected, captured.err
        return json.loads(captured.out if code == 0 else captured.err)
    return run


@pytest.fixture
def native_inputs(campaign_inputs, tmp_path, monkeypatch):
    source, data, root = campaign_inputs
    config = tmp_path / "package" / "config" / "TOP_26_006.yml"
    package_config = yaml.safe_load(config.read_text())
    package_config["wc_population"]["scientific"] = ["ctG"]
    package_config["wc_ranges"] = {"ctG": [-1.6, 1.6]}
    config.write_text(yaml.safe_dump(package_config))
    data["package"]["sha256"] = sha256_file(config)
    data["selection"]["wcs"] = ["ctG"]
    data["grid"]["selected_indices"] = [3]
    data["backend"] = "local"
    release_src = tmp_path / "release" / "src"
    release_src.mkdir(parents=True)
    fake_bin = release_src.parent / "bin" / "synthetic_arch"
    fake_bin.mkdir(parents=True)
    setup = tmp_path / "cmsset_default.sh"
    setup.write_text("[[ \"$SCRAM_ARCH\" == synthetic_arch ]] || return 9\n"
        "cmsenv() {\n" +
        "  [[ \"$(pwd -P)\" == {} ]] || return 10\n".format(shlex.quote(str(release_src.resolve()))) +
        "  export CMSSW_BASE={}\n".format(shlex.quote(str(release_src.parent))) +
        "  export PATH={}:$PATH\n".format(shlex.quote(str(fake_bin))) +
        "}\n")
    data["local_runtime"] = {"cmsset_default": str(setup), "scram_arch": "synthetic_arch",
                             "release_src": str(release_src)}
    source.write_text(yaml.safe_dump(data))
    spec, _ = load_spec(source)
    coordinate = next(point["coordinate"] for point in planned_points(spec)
                      if point["point_index"] == data["grid"]["selected_indices"][0])
    valid_root = tmp_path / "valid.root"
    numeric_root = tmp_path / "numeric.root"
    for path, delta in ((valid_root, 1.0), (numeric_root, 9990.0)):
        with uproot.recreate(path) as output:
            tree = output.mktree("limit", {"ctG": "float32", "deltaNLL": "float32",
                                         "quantileExpected": "float32"})
            tree.extend({"ctG": np.array([coordinate], dtype="f4"),
                         "deltaNLL": np.array([delta], dtype="f4"),
                         "quantileExpected": np.array([0], dtype="f4")})
    invalid_root = tmp_path / "invalid.root"
    invalid_root.write_bytes(b"invalid ROOT bytes")
    mode = tmp_path / "worker_mode.txt"
    mode.write_text("success")
    combine = fake_bin / "combine"
    combine.write_text("#!/bin/sh\nset -eu\n"
        "[ \"$SCRAM_ARCH\" = synthetic_arch ]\n"
        "[ \"$CMSSW_BASE\" = {} ]\n".format(shlex.quote(str(release_src.parent))) +
        "[ \"$(ulimit -s)\" = unlimited ]\n"
        "[ -z \"${PYTHONPATH+x}\" ] && [ -z \"${CONDA_PREFIX+x}\" ] && [ -z \"${LD_LIBRARY_PATH+x}\" ]\n"
        "printf '%s\\n' \"$@\" > argv.txt\n"
        "printf 'python=%s conda=%s library=%s stack=%s\\n' \"${PYTHONPATH-unset}\" \"${CONDA_PREFIX-unset}\" \"${LD_LIBRARY_PATH-unset}\" \"$(ulimit -s)\" > worker_env.txt\n"
        "label=\nwhile [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = -n ]; then shift; label=\"$1\"; fi\n"
        "  shift\ndone\n" +
        "mode=$(cat {})\n".format(shlex.quote(str(mode))) +
        "[ \"$mode\" != nonzero ] || exit 17\n"
        "[ \"$mode\" != missing ] || exit 0\n"
        "case \"$mode\" in\n"
        "  invalid) source={} ;;\n".format(shlex.quote(str(invalid_root))) +
        "  numerical) source={} ;;\n".format(shlex.quote(str(numeric_root))) +
        "  *) source={} ;;\n".format(shlex.quote(str(valid_root))) +
        "esac\ncp -- \"$source\" \"higgsCombine${label}.MultiDimFit.mH120.root\"\n")
    combine.chmod(0o700)
    monkeypatch.setenv("PYTHONPATH", "poisoned_controller")
    monkeypatch.setenv("CONDA_PREFIX", "poisoned_controller")
    monkeypatch.setenv("LD_LIBRARY_PATH", "poisoned_controller")
    return source, root, mode, coordinate


@pytest.fixture
def invoke_native(capsys, native_inputs):
    _, root, _, _ = native_inputs
    def run(*args, expected=0):
        code = cli.main(["--root", str(root), "--json", *map(str, args)], backends=None)
        captured = capsys.readouterr()
        assert code == expected, captured.err
        return json.loads(captured.out if code == 0 else captured.err)
    return run


def test_native_local_singleton_cli_and_009a_receipt(native_inputs, invoke_native):
    source, root, _, coordinate = native_inputs
    created = invoke_native("create", source)
    campaign_id = created["campaign_id"]
    preview = invoke_native("plan", campaign_id, "--dry-run")
    assert preview["request"]["attempt_id"] == "0" * 32
    assert invoke_native("status", campaign_id)["attempts"] == []
    planned = invoke_native("plan", campaign_id)
    attempt_id = planned["request"]["attempt_id"]
    job = planned["plan"]["jobs"][0]
    assert attempt_id != "0" * 32
    assert job["point_indices"] == [3]
    assert job["execution_provenance"]["original_points"] == 5
    assert coordinate == pytest.approx(0.8)
    assert job["command_argvs"][0].count("--points") == 1
    assert job["command_argvs"][0][job["command_argvs"][0].index("--points") + 1] == "5"
    assert job["command_argvs"][0][-6:-1] == ["--firstPoint", "3", "--lastPoint", "3", "-n"]
    condor_preview = invoke_native("plan", campaign_id, "--backend", "condor", "--dry-run")
    assert condor_preview["plan"]["jobs"][0]["job_set_id"] == job["job_set_id"]
    invoke_native("dispatch", campaign_id, attempt_id)
    assert invoke_native("status", campaign_id)["attempts"][0]["runtime"]["provider"] == "local"
    collected = invoke_native("collect", campaign_id, attempt_id)
    receipt = collected["receipts"][0]
    assert receipt["status"] == "accepted"
    assert receipt["execution_link"]["job_set_id"] == job["job_set_id"]
    assert len(receipt["references"]) == 4
    manifest = read_json(receipt["references"][0]["resolved_path"])
    record = read_json(receipt["references"][1]["resolved_path"])
    worker_result = read_json(receipt["references"][2]["resolved_path"])
    assert manifest["schema"] == 5 and manifest["selected_indices"] == [3]
    assert record["attempt_id"] == "campaign_" + attempt_id
    assert record["member_set"] == manifest["expected_members"]
    assert worker_result["resolved_runtime"]["combine_executable"] == str(source.parent / "release" / "bin" / "synthetic_arch" / "combine")
    assert worker_result["resolved_runtime"]["CMSSW_BASE"] == str(source.parent / "release")
    assert worker_result["resolved_runtime"]["SCRAM_ARCH"] == "synthetic_arch"
    assert worker_result["resolved_runtime"]["stack"] == "unlimited"
    work_directory = Path(manifest["scheduler_directory"]) / record["attempt_id"] / "member_0000"
    assert (work_directory / "argv.txt").read_text().splitlines() == job["command_argvs"][0][1:]
    assert "python=unset conda=unset library=unset stack=unlimited" in (work_directory / "worker_env.txt").read_text()
    assert invoke_native("status", campaign_id)["points"][3]["status"] == "accepted"
    assert "already dispatched" in invoke_native("dispatch", campaign_id, attempt_id, expected=2)["error"]
    assert "already collected" in invoke_native("collect", campaign_id, attempt_id, expected=2)["error"]
    assert invoke_native("retry", campaign_id)["status"] == "no_eligible_points"


@pytest.mark.parametrize("mode, expected_status", [("nonzero", "invalid"),
    ("missing", "missing"), ("invalid", "invalid"), ("numerical", "invalid")])
def test_native_local_terminal_retry(native_inputs, invoke_native, mode, expected_status):
    source, _, mode_path, _ = native_inputs
    mode_path.write_text(mode)
    campaign_id = invoke_native("create", source)["campaign_id"]
    planned = invoke_native("plan", campaign_id)
    attempt_id = planned["request"]["attempt_id"]
    invoke_native("dispatch", campaign_id, attempt_id)
    receipt = invoke_native("collect", campaign_id, attempt_id)["receipts"][0]
    assert receipt["status"] == expected_status
    if mode == "nonzero":
        assert "nonzero" in receipt["reason"] and "17" in receipt["reason"]
    if mode == "numerical":
        assert "invalid evaluation point" in receipt["reason"]
        rescue = invoke_native("rescue", campaign_id, "--strategy", "minimizer_strategy_1")
        next_attempt = rescue["request"]["attempt_id"]
    else:
        retry = invoke_native("retry", campaign_id)
        next_attempt = retry["request"]["attempt_id"]
    assert next_attempt != attempt_id
    mode_path.write_text("success")
    invoke_native("dispatch", campaign_id, next_attempt)
    recovered = invoke_native("collect", campaign_id, next_attempt)["receipts"][0]
    assert recovered["status"] == "accepted"
    if mode == "numerical":
        assert recovered["execution_link"]["job_set_id"] != planned["plan"]["jobs"][0]["job_set_id"]
    else:
        assert recovered["execution_link"]["job_set_id"] == planned["plan"]["jobs"][0]["job_set_id"]
    assert recovered["references"][-1]["resolved_path"] != receipt["references"][-1]["resolved_path"]


def test_native_local_collision_and_unknown_intent(native_inputs, invoke_native, monkeypatch):
    source, root, _, _ = native_inputs
    campaign_id = invoke_native("create", source)["campaign_id"]
    plan = invoke_native("plan", campaign_id)
    attempt_id = plan["request"]["attempt_id"]
    manifest = plan["plan"]["jobs"][0]["execution_provenance"]
    destination = Path(manifest["execution_directory"]) / manifest["expected_members"][0]["file_name"]
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"foreign output")
    assert "already exists" in invoke_native("dispatch", campaign_id, attempt_id, expected=2)["error"]
    assert destination.read_bytes() == b"foreign output"
    assert "unknown local worker state" in invoke_native("collect", campaign_id, attempt_id, expected=2)["error"]
    assert invoke_native("status", campaign_id)["points"][3]["status"] == "running"
    assert invoke_native("retry", campaign_id)["status"] == "no_eligible_points"

    second = invoke_native("plan", campaign_id, "--dry-run")
    assert second["status"] == "no_eligible_points"
    next_campaign = invoke_native("create", source)["campaign_id"]
    next_attempt = invoke_native("plan", next_campaign)["request"]["attempt_id"]
    with monkeypatch.context() as patch:
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt
        patch.setattr(campaign_backend.subprocess, "run", interrupted)
        with pytest.raises(KeyboardInterrupt):
            cli.main(["--root", str(root), "dispatch", next_campaign, next_attempt], backends=None)
    assert "unknown local worker state" in invoke_native("collect", next_campaign, next_attempt, expected=2)["error"]
    assert invoke_native("status", next_campaign)["points"][3]["status"] == "running"


def test_native_local_requires_frozen_runtime_before_intent(native_inputs, invoke_native):
    source, _, _, _ = native_inputs
    data = yaml.safe_load(source.read_text())
    del data["local_runtime"]
    source.write_text(yaml.safe_dump(data))
    campaign_id = invoke_native("create", source)["campaign_id"]
    attempt_id = invoke_native("plan", campaign_id)["request"]["attempt_id"]
    assert "requires local_runtime" in invoke_native("dispatch", campaign_id, attempt_id, expected=2)["error"]
    assert invoke_native("status", campaign_id)["points"][3]["status"] == "planned"


def test_native_local_cmsenv_failure_cannot_run_worker(native_inputs, invoke_native):
    source, _, _, _ = native_inputs
    setup = Path(yaml.safe_load(source.read_text())["local_runtime"]["cmsset_default"])
    setup.write_text(setup.read_text().replace("  export CMSSW_BASE=", "  echo 'synthetic cmsenv failure' >&2\n  return 9\n  export CMSSW_BASE="))
    campaign_id = invoke_native("create", source)["campaign_id"]
    attempt_id = invoke_native("plan", campaign_id)["request"]["attempt_id"]
    invoke_native("dispatch", campaign_id, attempt_id)
    receipt = invoke_native("collect", campaign_id, attempt_id)["receipts"][0]
    assert receipt["status"] == "invalid" and "nonzero" in receipt["reason"]
    assert receipt["reference_group"] is None


@pytest.mark.parametrize("base_mode", ["missing", "wrong_release"])
def test_native_local_rejects_unbound_release(native_inputs, invoke_native, base_mode):
    source, _, _, _ = native_inputs
    setup = Path(yaml.safe_load(source.read_text())["local_runtime"]["cmsset_default"])
    text = setup.read_text()
    if base_mode == "missing":
        text = text.replace("  export CMSSW_BASE=", "  unset CMSSW_BASE\n  # export CMSSW_BASE=")
    else:
        wrong_release = source.parent / "wrong_release"
        (wrong_release / "src").mkdir(parents=True)
        text = text.replace("  export CMSSW_BASE=", "  export CMSSW_BASE={}\n  # export CMSSW_BASE=".format(shlex.quote(str(wrong_release))))
    setup.write_text(text)
    campaign_id = invoke_native("create", source)["campaign_id"]
    attempt_id = invoke_native("plan", campaign_id)["request"]["attempt_id"]
    invoke_native("dispatch", campaign_id, attempt_id)
    receipt = invoke_native("collect", campaign_id, attempt_id)["receipts"][0]
    result = read_json(receipt["references"][2]["resolved_path"])
    assert result["status"] == "runtime_unresolved"
    assert result["exit_code"] != 0
    assert "configured release" in Path(result["stderr"]).read_text()
    assert receipt["status"] == "invalid"
    assert receipt["reference_group"] is None
    assert invoke_native("status", campaign_id)["points"][3]["status"] == "invalid"


def test_native_local_rejects_foreign_combine_before_execution(native_inputs, invoke_native):
    source, _, _, _ = native_inputs
    setup = Path(yaml.safe_load(source.read_text())["local_runtime"]["cmsset_default"])
    foreign_bin = source.parent / "foreign_bin"
    foreign_bin.mkdir()
    foreign_combine = foreign_bin / "combine"
    foreign_combine.write_text("#!/bin/sh\nexit 0\n")
    foreign_combine.chmod(0o700)
    release_bin = source.parent / "release" / "bin" / "synthetic_arch"
    setup.write_text(setup.read_text().replace(
        "export PATH={}:$PATH".format(shlex.quote(str(release_bin))),
        "export PATH={}:$PATH".format(shlex.quote(str(foreign_bin)))))
    campaign_id = invoke_native("create", source)["campaign_id"]
    attempt_id = invoke_native("plan", campaign_id)["request"]["attempt_id"]
    invoke_native("dispatch", campaign_id, attempt_id)
    receipt = invoke_native("collect", campaign_id, attempt_id)["receipts"][0]
    result = read_json(receipt["references"][2]["resolved_path"])
    assert result["status"] == "runtime_unresolved"
    assert result["exit_code"] != 0
    assert result.get("resolved_runtime") is None
    assert "configured release" in Path(result["stderr"]).read_text()
    assert receipt["status"] == "invalid" and receipt["reference_group"] is None


def test_native_local_publication_error_after_zero_exit(native_inputs, invoke_native, monkeypatch):
    source, root, _, _ = native_inputs
    campaign_id = invoke_native("create", source)["campaign_id"]
    planned = invoke_native("plan", campaign_id)
    attempt_id = planned["request"]["attempt_id"]
    member = planned["plan"]["jobs"][0]["expected_members"][0]
    destination = Path(planned["plan"]["jobs"][0]["execution_provenance"]["execution_directory"]) / member["file_name"]
    with monkeypatch.context() as patch:
        original_link = campaign_backend.os.link
        def fail_publication(*args, **kwargs):
            if Path(args[1]) == destination:
                raise OSError(28, "synthetic no space")
            return original_link(*args, **kwargs)
        patch.setattr(campaign_backend.os, "link", fail_publication)
        invoke_native("dispatch", campaign_id, attempt_id)
    receipt = invoke_native("collect", campaign_id, attempt_id)["receipts"][0]
    result = read_json(receipt["references"][2]["resolved_path"])
    assert result["status"] == "publication_failed" and result["exit_code"] == 0
    assert "synthetic no space" in result["error"]
    assert not destination.exists()
    assert receipt["status"] == "invalid" and "publication_failed" in receipt["reason"]
    assert receipt["reference_group"] is None
    assert invoke_native("status", campaign_id)["points"][3]["status"] == "invalid"


@pytest.mark.parametrize("tamper", ["manifest", "member"])
def test_native_local_rejects_foreign_mapping(native_inputs, invoke_native, tamper):
    source, root, _, _ = native_inputs
    campaign_id = invoke_native("create", source)["campaign_id"]
    attempt_id = invoke_native("plan", campaign_id)["request"]["attempt_id"]
    invoke_native("dispatch", campaign_id, attempt_id)
    mapping_path = root / campaign_id / "execution" / attempt_id / "mapping.json"
    mapping = read_json(mapping_path)
    if tamper == "manifest":
        Path(mapping[0]["manifest"]["resolved_path"]).write_text("{}")
    else:
        mapping[0]["member"]["first_point"] = 1
        mapping_path.write_text(json.dumps(mapping))
    result = invoke_native("collect", campaign_id, attempt_id, expected=2)
    assert "error" in result
    assert invoke_native("status", campaign_id)["points"][3]["status"] == "running"


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
    assert [p["status"] for p in observed["points"]] == ["accepted", "invalid", "invalid", "unresolved", "accepted"]
    assert first["request"]["points"][1]["point_index"] == 1
    before_condor = snapshot_tree(root)
    condor_retry = invoke("retry", campaign_id, "--backend", "condor", "--dry-run",
                          backend_map={"condor": condor_plan_backend()})
    assert condor_retry["plan"]["jobs"][0]["point_indices"] == [1, 2, 3]
    assert len(condor_retry["plan"]["jobs"][0]["condor"]["queue"]) == 1
    assert snapshot_tree(root) == before_condor
    rescue = invoke("rescue", campaign_id, "--backend", "provider_b", "--strategy", "minimizer_strategy_1", "--dry-run")
    assert [p["point_index"] for p in rescue["request"]["points"]] == [1]
    assert rescue["request"]["numerical_difference"] == {"--cminDefaultMinimizerStrategy": {"before": 0, "after": 1}}
    assert "--cminDefaultMinimizerStrategy=1" in rescue["plan"]["jobs"][0]["argv"]
    source.write_text("changed: original YAML no longer controls this campaign\n")
    retry, recovered = run_attempt(invoke, campaign_id, "provider_b", "retry")
    assert [p["point_index"] for p in retry["request"]["points"]] == [1, 2, 3]
    assert {r["reason"] for r in recovered["receipts"]} == {"PROMOTION_UNQUALIFIED_REFERENCE"}
    assert set(j["job_set_id"] for j in first["plan"]["jobs"]).isdisjoint(j["job_set_id"] for j in retry["plan"]["jobs"])
    assert [job["point_indices"] for job in retry["plan"]["jobs"]] == [[1, 2, 3]]
    observed = invoke("status", campaign_id)
    assert observed["campaign_id"] == campaign_id
    assert observed["active_generation_id"] == initial["active_generation_id"]
    assert observed["spec_digest"] == initial["spec_digest"]
    assert [p["status"] for p in observed["points"]] == ["accepted", "unresolved", "unresolved", "unresolved", "accepted"]
    assert observed["attempts"][-1]["runtime"]["provider"] == "provider_b"
    before = snapshot_tree(root)
    assert invoke("retry", campaign_id, "--backend", "provider_b")["status"] == "no_eligible_points"
    rescue_after = invoke("rescue", campaign_id, "--backend", "provider_b", "--strategy", "minimizer_strategy_1")
    assert rescue_after["status"] == "no_eligible_points"
    assert {p["reason"] for p in rescue_after["not_yet_recoverable"] if p["point_index"] in (1, 2, 3)} == {"promotion_unqualified_reference"}
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
    assert all(p["status"] == "accepted" and "evidence_issue" in p
               for p in invoke("status", campaign_id)["points"])
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
    assert plan["plan"]["capabilities"] == {"plan": True, "dispatch": True, "collect": True}
    jobs = plan["plan"]["jobs"]
    assert len({job["job_set_id"] for job in jobs}) == len(jobs)
    assert len({job["execution_provenance"]["output_label"] for job in jobs}) == len(jobs)
    assert len({(job["scan_id"], index) for job in jobs for index in job["point_indices"]}) == 26 * 2 * 5
    assert all(job["point_indices"] == list(range(5)) for job in jobs)
    job = plan["plan"]["jobs"][0]
    assert job["command_argvs"][0][0] == "combine"
    assert job["command_argvs"][0][job["command_argvs"][0].index("--firstPoint") + 1] == "0"
    assert job["command_argvs"][0][job["command_argvs"][0].index("--lastPoint") + 1] == "4"
    assert job["point_indices"] == list(range(5))
    actual = invoke("plan", campaign_id, "--backend", "condor", backend_map=None)
    assert "requires condor_runtime" in invoke("dispatch", campaign_id, actual["request"]["attempt_id"], backend_map=None, expected=2)["error"]
    assert all(p["status"] == "planned" for p in invoke("status", campaign_id, backend_map={})["points"])
    record_property("full_population_plan", json.dumps({"scan_ids": sorted({p["scan_id"] for p in initial["points"]}),
        "point_count": len(initial["points"]), "job_count": len(plan["plan"]["jobs"]),
        "capabilities": plan["plan"]["capabilities"], "example_job": plan["plan"]["jobs"][0]}, sort_keys=True))


def test_script_entrypoint_through_wrapper(campaign_inputs):
    source, _, root = campaign_inputs
    command = ["/users/apiccine/work/correction-lib/codex-run.sh", sys.executable, str(cli_path), "--root", str(root)]
    result = subprocess.run(command + ["--json", "create", str(source)], text=True, capture_output=True, check=True)
    initial = json.loads(result.stdout)
    result = subprocess.run(command + ["--json", "status", initial["campaign_id"]], text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == initial


def test_default_cli_summarizes_plan_and_json_remains_explicit(campaign_inputs, capsys):
    source, _, root = campaign_inputs
    created = cli.manager.create(root, source)
    campaign_id = created["campaign_id"]
    assert cli.main(["--root", str(root), "plan", campaign_id, "--backend", "local", "--dry-run"]) == 0
    readable = capsys.readouterr().out
    assert "selected points: 5" in readable
    assert "5 per member" not in readable and "all per member" in readable
    assert "1 member" in readable and "next action:" in readable
    assert cli.main(["--root", str(root), "--json", "plan", campaign_id,
                     "--backend", "local", "--dry-run"]) == 0
    machine = json.loads(capsys.readouterr().out)
    assert machine["request"]["campaign_id"] == campaign_id


def test_prepare_reference_cli_uses_injected_fake_provider(campaign_inputs, providers,
                                                            invoke, monkeypatch, capsys):
    source, data, root = campaign_inputs
    data["grid"]["points"] = 7
    source.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(condor_plan_backend, "prepare_reference", fake_prepare_reference)
    campaign_id = invoke("create", source)["campaign_id"]
    result = invoke("prepare-reference", campaign_id, "--scan-id", "c_one:profiled",
                    "--backend", "provider_a")
    assert result["state"] == "ready"
    assert result["reference"]["runtime"]["synthetic"] is True
    assert cli.main(["--root", str(root), "prepare-reference", campaign_id,
                     "--scan-id", "c_one:profiled", "--backend", "provider_a"],
                    backends=providers) == 0
    readable = capsys.readouterr().out
    assert "campaign: " + campaign_id in readable and "reference: already_ready" in readable


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
    assert {p["status"] for p in observed["points"] if p["wc"] == "c_one"} == {"accepted"}
    assert all("evidence_issue" in p for p in observed["points"] if p["wc"] == "c_one")
    assert {p["status"] for p in observed["points"] if p["wc"] == "c_two"} == {"accepted"}
    retry = invoke("retry", campaign_id, "--backend", "provider_b", "--dry-run")
    assert retry["status"] == "no_eligible_points"
    assert {p["reason"] for p in retry["not_yet_recoverable"] if p["scan_id"] == "c_one:profiled"} == {"accepted_valid_candidate"}

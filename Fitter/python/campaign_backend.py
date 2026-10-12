"""Provider-neutral 009A direct-grid planning and a local worker adapter."""

from datetime import datetime, timezone
import os
import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .execution_provenance import (build_direct_attempt_record, build_direct_grid_provenance,
                                   file_identity, read_json, sha256_file,
                                   validate_direct_attempt_record, validate_direct_grid_provenance,
                                   write_json_exclusive)
from .wc_fit_config import build_configured_grid_scan_command
from .campaign_spec import digest, fit_config, planned_points


def runtime_identity():
    return {"python_executable": sys.executable, "python_version": platform.python_version(),
            "platform": platform.platform(), "execution_environment": "not_resolved_no_submit",
            "source_sha256": {name: sha256_file(Path(__file__).with_name(name)) for name in (
                "campaign_backend.py", "campaign_spec.py", "campaign_state.py", "campaign_manager.py",
                "execution_provenance.py", "wc_fit_config.py", "inference_recipe.py", "scan_1d.py")}}


def grid_argv(spec, point, strategy):
    dataset = spec["dataset"]
    if dataset["mode"] == "observed":
        dataset_options = ["--dataset", dataset["name"]]
    elif dataset["mode"] == "expected" and dataset["truth"] == "sm" and dataset["nuisance_generation"] == "nominal":
        dataset_options = ["-t", "-1"]
    else:
        raise ValueError("unsupported or unfrozen campaign dataset policy")
    args = build_configured_grid_scan_command(fit_config(spec), spec["workspace"]["resolved_path"],
        name="", freeze=point["mode"] == "frozen", scan_params=[point["wc"]],
        points=spec["grid"]["points"], other=dataset_options + [
        "--alignEdges", "1" if spec["grid"]["align_edges"] else "0"])
    args[0] = "combine"
    if strategy == "minimizer_strategy_1":
        args[args.index("--cminDefaultMinimizerStrategy=0")] = "--cminDefaultMinimizerStrategy=1"
    return args


def _render_condor(provenance, attempt_id):
    """Render literal argv into a bounded shell dispatcher and exact queue."""
    attempt_directory = os.path.join(provenance["scheduler_directory"], "campaign_" + attempt_id)
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", attempt_directory):
        raise ValueError("Condor submit paths require simple absolute path components")
    script_path = os.path.join(attempt_directory, "worker.sh")
    lines = ["#!/bin/sh\n", "set -eu\n",
             "test \"$(sha256sum -- {} | cut -d ' ' -f 1)\" = {}\n".format(
                 shlex.quote(provenance["workspace_identity"]["resolved_path"]),
                 shlex.quote(provenance["workspace_identity"]["sha256"])),
             "case \"$1\" in\n"]
    queue = []
    for member, argv in zip(provenance["expected_members"], provenance["command_argvs"]):
        index = member["member_index"]
        work_directory = os.path.join(attempt_directory, "member_{:04d}".format(index))
        destination = os.path.join(provenance["execution_directory"], member["file_name"])
        queue.append({"member_index": index, "work_directory": work_directory})
        lines.extend(["  {})\n".format(index),
                      "    {}\n".format(shlex.join(argv)),
                      "    test -s {}\n".format(shlex.quote(member["file_name"])),
                      "    test ! -e {}\n".format(shlex.quote(destination)),
                      "    ln -- {} {}\n".format(shlex.quote(member["file_name"]),
                                                  shlex.quote(destination)),
                      "    ;;\n"])
    lines.extend(["  *) exit 2 ;;\n", "esac\n"])
    submit = ["universe = vanilla\n", "executable = {}\n".format(script_path),
              "arguments = $(member_index)\n", "initialdir = $(work_directory)\n",
              "output = $(work_directory)/stdout.txt\n",
              "error = $(work_directory)/stderr.txt\n",
              "log = {}/condor.log\n".format(attempt_directory),
              "request_memory = 10000\n", "queue member_index, work_directory from (\n"]
    submit.extend("{} {}\n".format(item["member_index"], item["work_directory"])
                  for item in queue)
    submit.append(")\n")
    return {"worker_script_path": script_path, "worker_script": "".join(lines),
            "submit_description": "".join(submit), "queue": queue}


class condor_plan_backend:
    name = "condor"
    capabilities = {"plan": True, "dispatch": False, "collect": False}

    def plan(self, request, spec):
        expected = {(point["scan_id"], point["point_index"]): point
                    for point in planned_points(spec)}
        selected = {}
        for point in request["points"]:
            key = (point["scan_id"], point["point_index"])
            if key not in expected or point != expected[key] or key in selected:
                raise ValueError("invalid, duplicate or conflicting original-grid point")
            selected[key] = point
        if not selected:
            raise ValueError("direct-grid plan requires selected points")
        scans = {}
        for point in selected.values():
            scans.setdefault(point["scan_id"], []).append(point)
        jobs = []
        for scan_id, scan_points in sorted(scans.items()):
            point = scan_points[0]
            # A worker-runtime binding cannot change the physical job identity.
            scientific_digest = digest({key: value for key, value in spec.items()
                                        if key != "local_runtime"})
            scan_name = ".campaign_" + digest({"campaign_id": request["campaign_id"],
                "generation_id": request["generation_id"], "scan_id": scan_id,
                "spec_digest": scientific_digest})
            provenance = build_direct_grid_provenance(
                scan_name, grid_argv(spec, point, request["strategy"]), spec["workspace"],
                spec["grid"]["points"], [p["point_index"] for p in scan_points],
                request["execution_root"])
            validate_direct_grid_provenance(provenance, scan_name)
            attempt_directory = os.path.join(provenance["scheduler_directory"],
                                             "campaign_" + request["attempt_id"])
            if os.path.lexists(attempt_directory):
                raise ValueError("execution attempt directory already exists")
            if any(os.path.lexists(os.path.join(provenance["execution_directory"],
                                           member["file_name"]))
                   for member in provenance["expected_members"]):
                raise ValueError("identity-owned member output already exists")
            job = {"job_set_id": provenance["job_set_id"], "scan_name": scan_name,
                   "scan_id": scan_id, "point_indices": provenance["selected_indices"],
                   "expected_execution_attempt_id": "campaign_" + request["attempt_id"],
                   "execution_provenance": provenance,
                   "expected_members": provenance["expected_members"],
                   "command_argvs": provenance["command_argvs"],
                   "working_directories": [os.path.join(attempt_directory,
                       "member_{:04d}".format(member["member_index"]))
                       for member in provenance["expected_members"]]}
            if self.name == "condor":
                job["condor"] = _render_condor(provenance, request["attempt_id"])
            jobs.append(job)
        if len({job["job_set_id"] for job in jobs}) != len(jobs):
            raise ValueError("cross-scan job identity collision")
        return {"requested_backend": request["requested_backend"], "actual_backend": self.name,
                "runtime": runtime_identity(), "jobs": jobs, "capabilities": self.capabilities}


class local_plan_backend(condor_plan_backend):
    name = "local"
    capabilities = {"plan": True, "dispatch": True, "collect": True}

    def runtime(self, spec):
        binding = spec.get("local_runtime")
        if binding is None:
            raise ValueError("local dispatch requires local_runtime in the frozen campaign YAML")
        cmsset = Path(binding["cmsset_default"])
        release_src = Path(binding["release_src"])
        if not cmsset.is_file() or cmsset.is_symlink() or not release_src.is_dir():
            raise ValueError("local worker CMSSW runtime paths are unavailable or unsafe")
        return dict(runtime_identity(), provider=self.name,
                    execution_environment="clean_nonlogin_cmssw",
                    cmsset_default=file_identity(cmsset),
                    release_src=str(release_src.resolve()), scram_arch=binding["scram_arch"])

    def dispatch(self, attempt, spec, namespace):
        namespace = Path(namespace)
        if namespace != Path(attempt["execution_root"]):
            raise ValueError("local attempt namespace differs from reserved execution root")
        runtime = self.runtime(spec)
        if attempt["dispatch"]["runtime"] != runtime:
            raise ValueError("local worker runtime changed after dispatch intent")
        mapped = self.plan(attempt, spec)
        if [{key: job[key] for key in ("job_set_id", "scan_name", "scan_id", "point_indices",
                                   "expected_execution_attempt_id")} for job in mapped["jobs"]] != attempt["execution_links"]:
            raise ValueError("local execution differs from reserved 009A plan")
        namespace.mkdir(parents=True, mode=0o700)
        mapping = []
        for job in mapped["jobs"]:
            provenance = validate_direct_grid_provenance(job["execution_provenance"])
            execution_directory = Path(provenance["execution_directory"])
            manifest_path = execution_directory / "execution_provenance.json"
            write_json_exclusive(manifest_path, provenance)
            manifest_reference = file_identity(manifest_path)
            attempt_directory = Path(provenance["scheduler_directory"]) / (
                "campaign_" + attempt["attempt_id"])
            attempt_directory.mkdir(parents=True, mode=0o700)
            record_path = Path(provenance["attempts_directory"]) / (
                job["expected_execution_attempt_id"] + ".json")
            artifacts = []
            for member in provenance["expected_members"]:
                work_directory = attempt_directory / "member_{:04d}".format(member["member_index"])
                artifacts.extend(str(work_directory / name) for name in (
                    "worker_start.json", "worker_result.json", "runtime_resolution.txt",
                    "stdout.txt", "stderr.txt"))
            record = build_direct_attempt_record(provenance,
                job["expected_execution_attempt_id"], attempt["operation"],
                provenance["expected_members"], manifest_path, artifacts)
            validate_direct_attempt_record(provenance, record, manifest_reference["sha256"])
            write_json_exclusive(record_path, record)
            record_reference = file_identity(record_path)
            for member, argv in zip(provenance["expected_members"], provenance["command_argvs"]):
                work_directory = attempt_directory / "member_{:04d}".format(member["member_index"])
                work_directory.mkdir(mode=0o700)
                start_path = work_directory / "worker_start.json"
                result_path = work_directory / "worker_result.json"
                resolution_path = work_directory / "runtime_resolution.txt"
                stdout_path = work_directory / "stdout.txt"
                stderr_path = work_directory / "stderr.txt"
                write_json_exclusive(start_path, {"attempt_id": record["attempt_id"],
                    "job_set_id": job["job_set_id"], "member": member,
                    "command_argv": argv, "runtime": runtime,
                    "started_utc": datetime.now(timezone.utc).isoformat()})
                worker_script = ("set -euo pipefail -C\n"
                    "ulimit -s unlimited\n"
                    "source \"$1\"\n"
                    "export SCRAM_ARCH=\"$2\"\n"
                    "cd \"$3\"\n"
                    "runtime_exports=\"$(scram runtime -sh)\"\n"
                    "test -n \"$runtime_exports\"\n"
                    "eval \"$runtime_exports\"\n"
                    "resolved_combine=\"$(command -v combine)\"\n"
                    "test -n \"$resolved_combine\"\n"
                    "printf '%s\\n' \"SCRAM_ARCH=$SCRAM_ARCH\" \"CMSSW_BASE=${CMSSW_BASE-}\" \"combine_executable=$resolved_combine\" \"stack=$(ulimit -s)\" > \"$5\"\n"
                    "cd \"$4\"\n"
                    "shift 5\n"
                    "exec \"$@\"\n")
                worker_command = ["/bin/bash", "--noprofile", "--norc", "-c", worker_script,
                    "eftfit_local_worker", runtime["cmsset_default"]["resolved_path"],
                    runtime["scram_arch"], runtime["release_src"], str(work_directory),
                    str(resolution_path), *argv]
                worker_environment = {"PATH": "/usr/bin:/bin"}
                for key in ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP"):
                    if key in os.environ:
                        worker_environment[key] = os.environ[key]
                outcome = {"attempt_id": record["attempt_id"], "job_set_id": job["job_set_id"],
                           "member": member, "command_argv": argv, "runtime": runtime,
                           "stdout": str(stdout_path), "stderr": str(stderr_path),
                           "finished_utc": None, "exit_code": None, "status": "launch_failed"}
                try:
                    if file_identity(runtime["cmsset_default"]["resolved_path"]) != runtime["cmsset_default"]:
                        raise ValueError("local CMSSW setup changed after dispatch intent")
                    with stdout_path.open("x", encoding="utf-8") as stdout, stderr_path.open("x", encoding="utf-8") as stderr:
                        completed = subprocess.run(worker_command, cwd=work_directory,
                            env=worker_environment, stdout=stdout, stderr=stderr, check=False)
                    outcome["exit_code"] = completed.returncode
                    outcome["status"] = "completed" if completed.returncode == 0 else "nonzero"
                    if completed.returncode == 0:
                        try:
                            resolved_runtime = dict(line.split("=", 1) for line in
                                resolution_path.read_text(encoding="utf-8").splitlines())
                        except (OSError, ValueError):
                            resolved_runtime = {}
                        if (set(resolved_runtime) != {"SCRAM_ARCH", "CMSSW_BASE", "combine_executable", "stack"} or
                                resolved_runtime["SCRAM_ARCH"] != runtime["scram_arch"] or
                                resolved_runtime["stack"] != "unlimited" or
                                not resolved_runtime["combine_executable"].startswith("/")):
                            outcome["status"] = "runtime_unresolved"
                        else:
                            outcome["resolved_runtime"] = resolved_runtime
                    if outcome["status"] == "completed":
                        worker_output = work_directory / member["file_name"]
                        destination = execution_directory / member["file_name"]
                        if worker_output.is_symlink() or (worker_output.exists() and not worker_output.is_file()):
                            outcome["status"] = "unsafe_output"
                        elif worker_output.is_file():
                            try:
                                os.link(worker_output, destination, follow_symlinks=False)
                            except FileExistsError:
                                outcome["status"] = "collision"
                except OSError as error:
                    outcome["error"] = str(error)
                outcome["finished_utc"] = datetime.now(timezone.utc).isoformat()
                write_json_exclusive(result_path, outcome)
                mapping.append({"job_set_id": job["job_set_id"],
                    "point_indices": list(range(member["first_point"], member["last_point"] + 1)),
                    "manifest": manifest_reference, "attempt_record": record_reference,
                    "member": member, "path": str(execution_directory / member["file_name"]),
                    "worker_result": file_identity(result_path)})
        write_json_exclusive(namespace / "mapping.json", mapping)

    def collect(self, attempt, namespace):
        namespace = Path(namespace)
        if namespace != Path(attempt["execution_root"]):
            raise ValueError("local attempt namespace differs from reserved execution root")
        mapping_path = namespace / "mapping.json"
        if not mapping_path.is_file() or mapping_path.is_symlink():
            raise RuntimeError("unknown local worker state: complete mapping is absent")
        mapping = read_json(mapping_path)
        if not isinstance(mapping, list) or not mapping:
            raise RuntimeError("unknown local worker state: invalid mapping")
        observed_members = set()
        expected_members = set()
        for item in mapping:
            link = next((link for link in attempt["execution_links"]
                         if link["job_set_id"] == item["job_set_id"]), None)
            if link is None or item["point_indices"] != list(range(
                    item["member"]["first_point"], item["member"]["last_point"] + 1)):
                raise ValueError("foreign local execution mapping")
            manifest = validate_direct_grid_provenance(read_json(item["manifest"]["resolved_path"]))
            record = read_json(item["attempt_record"]["resolved_path"])
            if (file_identity(item["manifest"]["resolved_path"]) != item["manifest"] or
                    file_identity(item["attempt_record"]["resolved_path"]) != item["attempt_record"] or
                    record["attempt_id"] != link["expected_execution_attempt_id"] or
                    item["member"] not in manifest["expected_members"] or
                    manifest["scan_name"] != link["scan_name"] or
                    manifest["selected_indices"] != link["point_indices"] or
                    manifest["execution_root"] != attempt["execution_root"] or
                    record["created_from"]["path"] != item["manifest"]["resolved_path"] or
                    item["path"] != str(Path(manifest["execution_directory"]) / item["member"]["file_name"])):
                raise ValueError("local 009A mapping identity changed")
            validate_direct_attempt_record(manifest, record, item["manifest"]["sha256"])
            expected_members.update((link["job_set_id"], member["member_index"])
                                    for member in record["member_set"])
            observed_members.add((link["job_set_id"], item["member"]["member_index"]))
            result_reference = item["worker_result"]
            if result_reference["resolved_path"] not in record["scheduler_artifacts"]:
                raise ValueError("local worker outcome is foreign to 009A attempt")
            if file_identity(result_reference["resolved_path"]) != result_reference:
                raise RuntimeError("unknown local worker state: outcome changed")
            result = read_json(result_reference["resolved_path"])
            if (result["attempt_id"] != record["attempt_id"] or
                    result["job_set_id"] != link["job_set_id"] or
                    result["member"] != item["member"] or
                    result["command_argv"] != manifest["command_argvs"][item["member"]["member_index"]] or
                    result["status"] not in ("completed", "nonzero", "launch_failed", "collision", "unsafe_output", "runtime_unresolved") or
                    (result["status"] == "completed" and (result["exit_code"] != 0 or
                        not isinstance(result.get("resolved_runtime"), dict))) or
                    (result["status"] == "nonzero" and (type(result["exit_code"]) is not int or result["exit_code"] == 0)) or
                    (result["status"] in ("collision", "unsafe_output", "runtime_unresolved") and result["exit_code"] != 0)):
                raise RuntimeError("unknown local worker state: outcome identity invalid")
            item["worker_status"] = result["status"]
            item["worker_exit_code"] = result["exit_code"]
        if observed_members != expected_members or {job_set_id for job_set_id, _ in observed_members} != {
                link["job_set_id"] for link in attempt["execution_links"]} or len(observed_members) != len(mapping):
            raise RuntimeError("unknown local worker state: incomplete or duplicate member mapping")
        return mapping


def select_backend(name, backends=None):
    providers = {"condor": condor_plan_backend(), "local": local_plan_backend()} if backends is None else backends
    if name is None:
        raise ValueError("select --backend explicitly or set backend in the campaign YAML")
    if name not in providers:
        raise ValueError("backend {!r} is unavailable; no fallback (shipped: condor planning and local execution)".format(name))
    provider = providers[name]
    if provider.name != name or not provider.capabilities.get("plan"):
        raise ValueError("provider identity or plan capability mismatch")
    return provider

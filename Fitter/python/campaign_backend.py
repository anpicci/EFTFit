"""Provider-neutral 009A direct-grid planning and worker adapters."""

from datetime import datetime, timezone
import os
import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import campaign_condor
from .execution_provenance import (build_direct_attempt_record, build_direct_grid_provenance,
                                   file_identity, read_json, sha256_file, workspace_identity,
                                   partition_grid_indices,
                                   validate_direct_attempt_record, validate_direct_grid_provenance,
                                   write_json_exclusive)
from .wc_fit_config import build_configured_grid_scan_command
from .campaign_spec import (default_condor_request_memory_mb, digest, fit_config,
                            planned_points, reference_contract)


def runtime_identity():
    return {"python_executable": sys.executable, "python_version": platform.python_version(),
            "platform": platform.platform(), "execution_environment": "not_resolved_no_submit",
            "source_sha256": {name: sha256_file(Path(__file__).with_name(name)) for name in (
                "campaign_backend.py", "campaign_condor.py", "campaign_spec.py", "campaign_state.py", "campaign_manager.py",
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


def reference_argv(spec, point, label):
    """The global fit generates the expected toy at zero before saving its snapshot."""
    args = grid_argv(spec, point, None)
    for option in ("--points", "--alignEdges"):
        index = args.index(option)
        del args[index:index + 2]
    args[args.index("grid")] = "none"
    args += ["--saveWorkspace", "--saveFitResult", "-n", label]
    if spec["dataset"]["mode"] == "expected":
        args.append("--saveToys")
    return args


def resolved_points_per_member(spec, provider_name):
    if "execution" not in spec:  # Frozen legacy effective specification.
        return None
    value = spec["execution"]["points_per_member"]
    return ("all" if provider_name == "local" else 5) if value == "auto" else value


def reference_required(spec, provider_name, selected_indices):
    if "reference" not in spec:
        return False
    if spec["reference"]["mode"] == "shared_snapshot":
        return True
    _, intervals = partition_grid_indices(spec["grid"]["points"], selected_indices,
        resolved_points_per_member(spec, provider_name))
    return len(intervals) > 1


def shared_member_argv(spec, point, strategy, reference):
    args = grid_argv(spec, point, strategy)
    args[args.index("-d") + 1] = reference["artifact"]["resolved_path"]
    # Combine loads --snapshotName before --setParameters. Leaving the zero
    # assignment here would replace the fitted state used for the NLL anchor.
    index = args.index("--setParameters")
    del args[index:index + 2]
    args += ["--snapshotName", "MultiDimFit", "--skipInitialFit"]
    if spec["dataset"]["mode"] == "expected":
        args += ["--toysFile", reference["artifact"]["resolved_path"]]
    return args


def _render_condor(provenance, attempt_id, request_memory_mb):
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
    submit = ["universe = vanilla\n", "should_transfer_files = NO\n",
              "request_memory = {}M\n".format(request_memory_mb),
              "executable = {}\n".format(script_path),
              "arguments = $(member_index)\n", "initialdir = $(work_directory)\n",
              "output = $(work_directory)/stdout.txt\n",
              "error = $(work_directory)/stderr.txt\n",
              "log = {}/condor.log\n".format(attempt_directory),
              "queue member_index, work_directory from (\n"]
    submit.extend("{} {}\n".format(item["member_index"], item["work_directory"])
                  for item in queue)
    submit.append(")\n")
    return {"worker_script_path": script_path, "worker_script": "".join(lines),
            "submit_description": "".join(submit), "queue": queue}


class condor_plan_backend:
    name = "condor"
    capabilities = {"plan": True, "dispatch": True, "collect": True}

    def prepare_reference(self, spec, argv, output_directory, output_path):
        """Run one declared reference through the existing clean CMSSW runtime."""
        runtime = (local_plan_backend().runtime(spec) if "local_runtime" in spec
                   else self.runtime(spec))
        output_directory = Path(output_directory)
        output_directory.parent.mkdir(mode=0o700, exist_ok=True)
        output_directory.mkdir(mode=0o700)
        stdout_path = output_directory / "stdout.txt"
        stderr_path = output_directory / "stderr.txt"
        resolution_path = output_directory / "runtime_resolution.txt"
        script = ("set -euo pipefail -C\n"
            "export SCRAM_ARCH=\"$2\"\nset +u\nsource \"$1\"\ncd \"$3\"\ncmsenv\nset -u\n"
            "ulimit -s unlimited\nulimit -c 0\nhash -r\n"
            "resolved_combine=\"$(command -v combine || true)\"\n"
            "printf '%s\\n' \"SCRAM_ARCH=$SCRAM_ARCH\" \"CMSSW_BASE=${CMSSW_BASE-}\" \"combine_executable=$resolved_combine\" > \"$5\"\n"
            "if [[ -z \"${CMSSW_BASE-}\" || \"$CMSSW_BASE\" != /* || ! -d \"$CMSSW_BASE/src\" || \"$(cd -- \"$CMSSW_BASE/src\" && pwd -P)\" != \"$(pwd -P)\" || \"$resolved_combine\" != /* || ! -x \"$resolved_combine\" || ! -x \"$CMSSW_BASE/bin/$2/combine\" || \"$(readlink -f -- \"$resolved_combine\")\" != \"$(readlink -f -- \"$CMSSW_BASE/bin/$2/combine\")\" ]]; then exit 1; fi\n"
            "cd \"$4\"\nshift 5\nexec \"$@\"\n")
        command = ["/bin/bash", "--noprofile", "--norc", "-c", script,
                   "eftfit_reference", runtime["cmsset_default"]["resolved_path"],
                   runtime["scram_arch"], runtime["release_src"], str(output_directory),
                   str(resolution_path), *argv]
        environment = {"PATH": "/usr/bin:/bin"}
        for key in ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP"):
            if key in os.environ:
                environment[key] = os.environ[key]
        if file_identity(runtime["cmsset_default"]["resolved_path"]) != runtime["cmsset_default"]:
            raise ValueError("reference CMSSW setup changed")
        with stdout_path.open("x", encoding="utf-8") as stdout, stderr_path.open("x", encoding="utf-8") as stderr:
            completed = subprocess.run(command, cwd=output_directory, env=environment,
                                       stdout=stdout, stderr=stderr, check=False)
        if completed.returncode != 0:
            raise RuntimeError("reference fit failed with exit code {}".format(completed.returncode))
        if Path(output_path).is_symlink() or not Path(output_path).is_file():
            raise ValueError("reference output is absent or unsafe")
        artifact = file_identity(output_path)
        if artifact["size_bytes"] == 0:
            raise ValueError("reference output is empty")
        import uproot
        label = argv[argv.index("-n") + 1]
        fit_result_path = output_directory / ("multidimfit" + label + ".root")
        if fit_result_path.is_symlink() or not fit_result_path.is_file():
            raise ValueError("reference fit result is absent or unsafe")
        try:
            with uproot.open(output_path) as source:
                if "w" not in source:
                    raise ValueError("reference output lacks the saved workspace")
                if spec["dataset"]["mode"] == "expected" and "toys/toy_asimov" not in source:
                    raise ValueError("reference output lacks the saved Asimov toy")
            with uproot.open(fit_result_path) as source:
                if "fit_mdf" not in source:
                    raise ValueError("reference output lacks the global fit result")
        except (OSError, KeyError) as error:
            raise ValueError("reference output is not a readable ROOT artifact") from error
        return {"artifact": artifact, "runtime": runtime,
                "fit_result": file_identity(fit_result_path),
                "stdout": file_identity(stdout_path), "stderr": file_identity(stderr_path),
                "runtime_resolution": file_identity(resolution_path)}

    def plan(self, request, spec, allow_existing=False):
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
            indices = [p["point_index"] for p in scan_points]
            reference = request.get("references", {}).get(scan_id)
            needs_reference = reference_required(spec, self.name, indices)
            if needs_reference and reference is None:
                raise ValueError("shared reference is not ready for " + scan_id + "; prepare-reference before plan")
            if reference is not None:
                if (reference.get("scan_id") != scan_id or
                        reference.get("scientific_contract") != reference_contract(spec, point)):
                    raise ValueError("shared reference scientific contract differs")
                reference_workspace = workspace_identity(reference["artifact"]["resolved_path"])
                if reference_workspace["sha256"] != reference["artifact"]["sha256"]:
                    raise ValueError("shared reference artifact changed")
                base_argv = shared_member_argv(spec, point, request["strategy"], reference)
            else:
                reference_workspace = spec["workspace"]
                base_argv = grid_argv(spec, point, request["strategy"])
            # A worker-runtime binding cannot change the physical job identity.
            scientific_digest = digest({key: value for key, value in spec.items()
                                        if key not in ("local_runtime", "condor_runtime")})
            scan_name = ".campaign_" + digest({"campaign_id": request["campaign_id"],
                "generation_id": request["generation_id"], "scan_id": scan_id,
                "spec_digest": scientific_digest})
            provenance = build_direct_grid_provenance(
                scan_name, base_argv, reference_workspace,
                spec["grid"]["points"], indices, request["execution_root"],
                resolved_points_per_member(spec, self.name), reference)
            validate_direct_grid_provenance(provenance, scan_name)
            attempt_directory = os.path.join(provenance["scheduler_directory"],
                                             "campaign_" + request["attempt_id"])
            if not allow_existing and os.path.lexists(attempt_directory):
                raise ValueError("execution attempt directory already exists")
            if not allow_existing and any(os.path.lexists(os.path.join(provenance["execution_directory"],
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
                memory = spec.get("condor_runtime", {}).get(
                    "request_memory_mb", default_condor_request_memory_mb)
                job["condor"] = _render_condor(provenance, request["attempt_id"], memory)
            jobs.append(job)
        if len({job["job_set_id"] for job in jobs}) != len(jobs):
            raise ValueError("cross-scan job identity collision")
        return {"requested_backend": request["requested_backend"], "actual_backend": self.name,
                "runtime": runtime_identity(), "jobs": jobs, "capabilities": self.capabilities}

    def runtime(self, spec):
        binding = spec.get("condor_runtime")
        if binding is None:
            raise ValueError("Condor dispatch requires condor_runtime in the frozen campaign YAML")
        cmsset = Path(binding["cmsset_default"])
        release_src = Path(binding["release_src"])
        if not cmsset.is_file() or cmsset.is_symlink() or not release_src.is_dir():
            raise ValueError("Condor worker CMSSW runtime paths are unavailable or unsafe")
        return dict(runtime_identity(), provider=self.name,
                    execution_environment="clean_nonlogin_cmssw_shared_filesystem",
                    cmsset_default=file_identity(cmsset),
                    release_src=str(release_src.resolve()), scram_arch=binding["scram_arch"],
                    request_memory_mb=binding.get("request_memory_mb", default_condor_request_memory_mb))

    def dispatch(self, attempt, spec, namespace, recover=False):
        namespace = Path(namespace)
        if namespace != Path(attempt["execution_root"]):
            raise ValueError("Condor attempt namespace differs from reserved execution root")
        runtime = self.runtime(spec)
        if attempt["dispatch"]["runtime"] != runtime:
            raise ValueError("Condor worker runtime changed after dispatch intent")
        mapped = self.plan(attempt, spec, allow_existing=recover)
        if [{key: job[key] for key in ("job_set_id", "scan_name", "scan_id", "point_indices",
                                   "expected_execution_attempt_id")} for job in mapped["jobs"]] != attempt["execution_links"]:
            raise ValueError("Condor execution differs from reserved 009A plan")
        if recover:
            for job in mapped["jobs"]:
                provenance = job["execution_provenance"]
                attempt_directory = Path(provenance["scheduler_directory"]) / (
                    "campaign_" + attempt["attempt_id"])
                if os.path.lexists(attempt_directory / "submission_response.json"):
                    raise ValueError("proven no-submit recovery has a scheduler binding")
                if any(os.path.lexists(Path(provenance["execution_directory"]) / member["file_name"])
                       for member in provenance["expected_members"]):
                    raise ValueError("proven no-submit recovery has member output")
        else:
            namespace.mkdir(parents=True, mode=0o700)
        submitted = 0
        for job in mapped["jobs"]:
            provenance = validate_direct_grid_provenance(job["execution_provenance"])
            execution_directory = Path(provenance["execution_directory"])
            manifest_path = execution_directory / "execution_provenance.json"
            attempt_directory = Path(provenance["scheduler_directory"]) / (
                "campaign_" + attempt["attempt_id"])
            record_path = Path(provenance["attempts_directory"]) / (
                job["expected_execution_attempt_id"] + ".json")
            worker_path = attempt_directory / "worker.sh"
            payload_path = attempt_directory / "worker_payload.json"
            submit_path = attempt_directory / "submit.sub"
            if recover and os.path.lexists(attempt_directory):
                if (attempt_directory.is_symlink() or read_json(manifest_path) != provenance):
                    raise ValueError("Condor recovery manifest differs from planned execution")
                record = read_json(record_path)
                manifest_reference = file_identity(manifest_path)
                validate_direct_attempt_record(provenance, record, manifest_reference["sha256"])
                intent = read_json(attempt_directory / "submission_intent.json")
                if (record["attempt_id"] != job["expected_execution_attempt_id"] or
                        record["member_set"] != provenance["expected_members"] or
                        intent != {"attempt_id": record["attempt_id"], "job_set_id": job["job_set_id"],
                            "manifest": manifest_reference, "attempt_record": file_identity(record_path),
                            "member_set": record["member_set"], "submit": file_identity(submit_path),
                            "worker_script": file_identity(worker_path),
                            "worker_payload": file_identity(payload_path)} or
                        read_json(payload_path) != {"manifest": str(manifest_path),
                            "record": str(record_path), "attempt_id": record["attempt_id"],
                            "runtime": runtime}):
                    raise ValueError("Condor recovery materialization differs from proven no-submit intent")
            else:
                write_json_exclusive(manifest_path, provenance)
                manifest_reference = file_identity(manifest_path)
                attempt_directory.mkdir(parents=True, mode=0o700)
                record = None
            artifacts = [str(attempt_directory / name) for name in (
                "worker.sh", "worker_payload.json", "submit.sub", "submission_intent.json", "submission_response.json")]
            for member in provenance["expected_members"]:
                work_directory = attempt_directory / "member_{:04d}".format(member["member_index"])
                if record is None:
                    work_directory.mkdir(mode=0o700)
                artifacts.extend(str(work_directory / name) for name in (
                    "worker_start.json", "worker_result.json", "runtime_resolution.txt",
                    "stdout.txt", "stderr.txt", "scheduler_stdout.txt", "scheduler_stderr.txt"))
            expected_record = build_direct_attempt_record(provenance,
                job["expected_execution_attempt_id"], attempt["operation"],
                provenance["expected_members"], manifest_path, artifacts)
            if record is not None and record != expected_record:
                raise ValueError("Condor recovery attempt record differs from planned execution")
            record = expected_record
            validate_direct_attempt_record(provenance, record, manifest_reference["sha256"])
            if not recover or not os.path.lexists(record_path):
                write_json_exclusive(record_path, record)
                write_json_exclusive(payload_path, {"manifest": str(manifest_path),
                    "record": str(record_path), "attempt_id": record["attempt_id"], "runtime": runtime})
            worker_text = ("#!/bin/sh\nset -eu\nexec /usr/bin/python3 {} worker {} \"$1\" \"$2\" \"$3\"\n".format(
                shlex.quote(str(Path(campaign_condor.__file__).resolve())), shlex.quote(str(payload_path))))
            if recover and worker_path.exists():
                if worker_path.read_text(encoding="utf-8") != worker_text:
                    raise ValueError("Condor recovery worker differs from planned execution")
            else:
                with worker_path.open("x", encoding="utf-8") as stream:
                    stream.write(worker_text)
                worker_path.chmod(0o700)
            projected_submit = job["condor"]["submit_description"]
            if projected_submit.count("request_memory = {}M\n".format(runtime["request_memory_mb"])) != 1 or sum(
                    line.startswith("request_memory") for line in projected_submit.splitlines()) != 1:
                raise ValueError("Condor submit memory request differs from dispatch runtime")
            if any(projected_submit.count(fragment) != 1 for fragment in (
                    "arguments = $(member_index)\n", "output = $(work_directory)/stdout.txt\n",
                    "error = $(work_directory)/stderr.txt\n")):
                raise ValueError("Condor submit projection changed before materialization")
            submit_text = projected_submit.replace(
                "arguments = $(member_index)\n", "arguments = $(member_index) $(ClusterId) $(ProcId)\n").replace(
                "output = $(work_directory)/stdout.txt\n", "output = $(work_directory)/scheduler_stdout.txt\n").replace(
                "error = $(work_directory)/stderr.txt\n", "error = $(work_directory)/scheduler_stderr.txt\n")
            if recover and submit_path.exists():
                if submit_path.read_text(encoding="utf-8") != submit_text:
                    raise ValueError("Condor recovery submit differs from planned execution")
            else:
                with submit_path.open("x", encoding="utf-8") as stream:
                    stream.write(submit_text)
                write_json_exclusive(attempt_directory / "submission_intent.json", {
                    "attempt_id": record["attempt_id"], "job_set_id": job["job_set_id"],
                    "manifest": manifest_reference, "attempt_record": file_identity(record_path),
                    "member_set": record["member_set"], "submit": file_identity(submit_path),
                    "worker_script": file_identity(worker_path), "worker_payload": file_identity(payload_path)})
            # A submission may have succeeded even if the call or response is lost.
            try:
                response = campaign_condor.submit_job(submit_path, len(record["member_set"]))
            except campaign_condor.submission_not_started as error:
                if submitted:
                    raise RuntimeError("Condor attempt has an earlier submitted job") from error
                raise
            submitted += 1
            write_json_exclusive(attempt_directory / "submission_response.json", response)

    def collect(self, attempt, namespace):
        namespace = Path(namespace)
        if namespace != Path(attempt["execution_root"]):
            raise ValueError("Condor attempt namespace differs from reserved execution root")
        mapping = []
        for link in attempt["execution_links"]:
            provenance_directory = namespace / ("direct" + link["scan_name"] + ".job_" + link["job_set_id"])
            manifest_path = provenance_directory / "execution_provenance.json"
            manifest = validate_direct_grid_provenance(read_json(manifest_path))
            if (manifest["job_set_id"] != link["job_set_id"] or
                    manifest["scan_name"] != link["scan_name"] or
                    manifest["selected_indices"] != link["point_indices"]):
                raise ValueError("Condor manifest differs from reserved execution link")
            manifest_reference = file_identity(manifest_path)
            attempt_directory = Path(manifest["scheduler_directory"]) / link["expected_execution_attempt_id"]
            record_path = Path(manifest["attempts_directory"]) / (link["expected_execution_attempt_id"] + ".json")
            record = read_json(record_path)
            validate_direct_attempt_record(manifest, record, manifest_reference["sha256"])
            if record["attempt_id"] != link["expected_execution_attempt_id"] or record["member_set"] != manifest["expected_members"]:
                raise ValueError("Condor attempt differs from reserved member set")
            intent = read_json(attempt_directory / "submission_intent.json")
            if (intent["attempt_id"] != record["attempt_id"] or
                    intent["job_set_id"] != link["job_set_id"] or
                    intent["manifest"] != manifest_reference or
                    intent["attempt_record"] != file_identity(record_path) or
                    intent["member_set"] != record["member_set"] or
                    intent["submit"] != file_identity(attempt_directory / "submit.sub") or
                    intent["worker_script"] != file_identity(attempt_directory / "worker.sh") or
                    intent["worker_payload"] != file_identity(attempt_directory / "worker_payload.json")):
                raise ValueError("Condor submission intent identity changed")
            response_path = attempt_directory / "submission_response.json"
            if not response_path.is_file() or response_path.is_symlink():
                raise RuntimeError("Condor submission outcome uncertain; binding response is absent")
            response = read_json(response_path)
            if (type(response.get("cluster_id")) is not int or
                    response.get("proc_ids") != list(range(len(record["member_set"]))) or
                    response.get("returncode") != 0 or
                    response.get("argv") != ["condor_submit", "-terse", str(attempt_directory / "submit.sub")] or
                    str(response_path) not in record["scheduler_artifacts"] or
                    campaign_condor.parse_submit_response(response.get("stdout"), len(record["member_set"])) !=
                    (response["cluster_id"], response["proc_ids"])):
                raise RuntimeError("Condor scheduler binding is malformed")
            for member in record["member_set"]:
                index = member["member_index"]
                scheduler_id = [response["cluster_id"], response["proc_ids"][index]]
                scheduler_row = campaign_condor.query_job(*scheduler_id)
                if (type(scheduler_row.get("ClusterId")) is not int or
                        type(scheduler_row.get("ProcId")) is not int or
                        scheduler_row["ClusterId"] != scheduler_id[0] or
                        scheduler_row["ProcId"] != scheduler_id[1] or
                        type(scheduler_row.get("JobStatus")) is not int):
                    raise RuntimeError("Condor scheduler outcome identity differs from binding")
                state = scheduler_row["JobStatus"]
                if state not in (3, 4):
                    raise RuntimeError("Condor member is live, held or uncertain: " + repr(scheduler_id))
                if state == 4 and type(scheduler_row.get("ExitCode")) is not int:
                    raise RuntimeError("Condor terminal exit is uncertain: " + repr(scheduler_id))
                work_directory = attempt_directory / "member_{:04d}".format(index)
                item = {"job_set_id": link["job_set_id"],
                    "point_indices": list(range(member["first_point"], member["last_point"] + 1)),
                    "manifest": manifest_reference, "attempt_record": file_identity(record_path),
                    "member": member, "path": str(provenance_directory / member["file_name"]),
                    "scheduler_id": scheduler_id, "scheduler_outcome": scheduler_row,
                    "submission_response": file_identity(response_path)}
                result_path = work_directory / "worker_result.json"
                if result_path.is_file() and not result_path.is_symlink():
                    if str(result_path) not in record["scheduler_artifacts"]:
                        raise ValueError("Condor worker result is foreign to attempt")
                    result = read_json(result_path)
                    if (result.get("attempt_id") != record["attempt_id"] or
                            result.get("job_set_id") != link["job_set_id"] or
                            result.get("member") != member or
                            result.get("command_argv") != manifest["command_argvs"][index] or
                            result.get("runtime") != attempt["dispatch"]["runtime"] or
                            result.get("scheduler_id") != scheduler_id or
                            result.get("status") not in ("completed", "nonzero", "launch_failed",
                                "collision", "unsafe_output", "runtime_unresolved", "publication_failed") or
                            (result.get("status") == "nonzero" and
                                (type(result.get("exit_code")) is not int or result["exit_code"] == 0)) or
                            (result.get("status") in ("collision", "unsafe_output", "publication_failed") and
                                result.get("exit_code") != 0) or
                            (result.get("status") == "publication_failed" and not result.get("error"))):
                        raise ValueError("Condor worker outcome identity invalid")
                    item["worker_result"] = file_identity(result_path)
                    item["worker_status"] = result["status"]
                    item["worker_exit_code"] = result["exit_code"]
                    if result["status"] == "completed" and (result["exit_code"] != 0 or
                            not isinstance(result.get("resolved_runtime"), dict)):
                        raise ValueError("Condor successful worker evidence is incomplete")
                elif state == 4 and scheduler_row.get("ExitCode") == 0:
                    raise RuntimeError("Condor terminal success lacks worker outcome evidence")
                else:
                    item["worker_status"] = "scheduler_failed"
                    item["worker_exit_code"] = scheduler_row.get("ExitCode")
                if state != 4 or (scheduler_row.get("ExitCode") != 0 and
                                  item["worker_status"] == "completed"):
                    item["worker_status"] = "scheduler_failed"
                    item["worker_exit_code"] = scheduler_row.get("ExitCode")
                mapping.append(item)
        return mapping


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
                    "export SCRAM_ARCH=\"$2\"\n"
                    "set +u\n"
                    "source \"$1\"\n"
                    "cd \"$3\"\n"
                    "cmsenv\n"
                    "set -u\n"
                    "ulimit -s unlimited\n"
                    "ulimit -c 0\n"
                    "hash -r\n"
                    "resolved_combine=\"$(command -v combine || true)\"\n"
                    "printf '%s\\n' \"SCRAM_ARCH=$SCRAM_ARCH\" \"CMSSW_BASE=${CMSSW_BASE-}\" \"combine_executable=$resolved_combine\" \"stack=$(ulimit -s)\" > \"$5\"\n"
                    "if [[ -z \"${CMSSW_BASE-}\" || \"$CMSSW_BASE\" != /* || ! -d \"$CMSSW_BASE/src\" || \"$(cd -- \"$CMSSW_BASE/src\" && pwd -P)\" != \"$(pwd -P)\" || \"$SCRAM_ARCH\" != \"$2\" || \"$resolved_combine\" != /* || ! -x \"$resolved_combine\" || ! -x \"$CMSSW_BASE/bin/$2/combine\" || \"$(readlink -f -- \"$resolved_combine\")\" != \"$(readlink -f -- \"$CMSSW_BASE/bin/$2/combine\")\" ]]; then\n"
                    "  printf 'local CMSSW runtime does not match configured release, architecture, or Combine executable\\n' >&2\n"
                    "  exit 1\n"
                    "fi\n"
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
                    if file_identity(provenance["workspace_identity"]["resolved_path"])["sha256"] != provenance["workspace_identity"]["sha256"]:
                        raise ValueError("local workspace/reference changed after planning")
                    with stdout_path.open("x", encoding="utf-8") as stdout, stderr_path.open("x", encoding="utf-8") as stderr:
                        completed = subprocess.run(worker_command, cwd=work_directory,
                            env=worker_environment, stdout=stdout, stderr=stderr, check=False)
                    outcome["exit_code"] = completed.returncode
                    outcome["status"] = "completed" if completed.returncode == 0 else "nonzero"
                    if completed.returncode == 0 or resolution_path.is_file():
                        try:
                            resolved_runtime = dict(line.split("=", 1) for line in
                                resolution_path.read_text(encoding="utf-8").splitlines())
                        except (OSError, ValueError):
                            resolved_runtime = {}
                        if (set(resolved_runtime) != {"SCRAM_ARCH", "CMSSW_BASE", "combine_executable", "stack"} or
                                resolved_runtime["SCRAM_ARCH"] != runtime["scram_arch"] or
                                resolved_runtime["stack"] != "unlimited" or
                                not resolved_runtime["CMSSW_BASE"] or
                                not Path(resolved_runtime["CMSSW_BASE"]).is_absolute() or
                                (Path(resolved_runtime["CMSSW_BASE"]) / "src").resolve() != Path(runtime["release_src"]).resolve() or
                                not Path(resolved_runtime["combine_executable"]).is_absolute() or
                                not os.access(resolved_runtime["combine_executable"], os.X_OK) or
                                Path(resolved_runtime["combine_executable"]).resolve() !=
                                (Path(resolved_runtime["CMSSW_BASE"]) / "bin" /
                                 runtime["scram_arch"] / "combine").resolve()):
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
                                outcome["status"] = "publication_failed"
                                outcome["error"] = str(error)
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
                    result["status"] not in ("completed", "nonzero", "launch_failed", "collision", "unsafe_output", "runtime_unresolved", "publication_failed") or
                    (result["status"] == "completed" and (result["exit_code"] != 0 or
                        not isinstance(result.get("resolved_runtime"), dict))) or
                    (result["status"] == "nonzero" and (type(result["exit_code"]) is not int or result["exit_code"] == 0)) or
                    (result["status"] == "runtime_unresolved" and type(result["exit_code"]) is not int) or
                    (result["status"] in ("collision", "unsafe_output", "publication_failed") and result["exit_code"] != 0) or
                    (result["status"] == "publication_failed" and not result.get("error"))):
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
        raise ValueError("backend {!r} is unavailable; no fallback (shipped: condor and local)".format(name))
    provider = providers[name]
    if provider.name != name or not provider.capabilities.get("plan"):
        raise ValueError("provider identity or plan capability mismatch")
    return provider

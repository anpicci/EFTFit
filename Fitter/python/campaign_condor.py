"""Bounded shared-filesystem Condor submission, query and direct-grid worker."""

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys

try:
    from .execution_provenance import (file_identity, read_json, validate_direct_attempt_record,
                                       validate_direct_grid_provenance, write_json_exclusive)
except ImportError:  # Direct worker script in the same package directory.
    from execution_provenance import (file_identity, read_json, validate_direct_attempt_record,
                                      validate_direct_grid_provenance, write_json_exclusive)


class submission_not_started(RuntimeError):
    """The submit executable never started, so this invocation created no job."""


def submit_job(submit_path, member_count):
    """Return actual cluster/proc identities; a lost response is ambiguous."""
    try:
        process = subprocess.Popen(["condor_submit", "-terse", str(submit_path)],
                                   cwd=submit_path.parent, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
    except OSError as error:
        raise submission_not_started("Condor submission did not start") from error
    try:
        stdout, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.communicate()
        raise RuntimeError("Condor submission outcome uncertain after timeout") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Condor submission outcome uncertain after invocation") from error
    response = {"argv": ["condor_submit", "-terse", str(submit_path)],
                "returncode": process.returncode, "stdout": stdout,
                "stderr": stderr}
    if process.returncode != 0:
        raise RuntimeError("Condor submission outcome uncertain: " + repr(response))
    cluster_id, proc_ids = parse_submit_response(stdout, member_count)
    return dict(response, cluster_id=cluster_id, proc_ids=proc_ids)


def parse_submit_response(stdout, member_count):
    if not isinstance(stdout, str) or len(stdout) > 256 or type(member_count) is not int or member_count < 1:
        raise RuntimeError("Condor submission response has invalid type or member count")
    match = re.fullmatch(r"\s*(\d+)\.(\d+)(?:\s*-\s*(\d+)\.(\d+))?\s*", stdout)
    if not match:
        raise RuntimeError("Condor submission response is incomplete or malformed: " + repr(stdout))
    cluster, first, last_cluster, last = match.groups()
    first = int(first)
    last = first if last is None else int(last)
    if (last_cluster is not None and last_cluster != cluster) or first != 0 or last != member_count - 1:
        raise RuntimeError("Condor submission cardinality differs from planned members: " + repr(stdout))
    return int(cluster), list(range(member_count))


def query_job(cluster_id, proc_id):
    """Query one exact scheduler identity; an empty answer remains unknown."""
    identity = "{}.{}".format(cluster_id, proc_id)
    for command in ("condor_q", "condor_history"):
        try:
            completed = subprocess.run([command, "-json", identity], capture_output=True,
                                       text=True, check=False, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("Condor outcome unknown for " + identity) from error
        if completed.returncode != 0:
            raise RuntimeError("Condor {} query failed for {}: {}".format(command, identity, completed.stderr))
        if isinstance(completed.stdout, str) and not completed.stdout.strip():
            rows = []
        else:
            try:
                import json
                rows = json.loads(completed.stdout)
            except (ValueError, TypeError) as error:
                raise RuntimeError("Condor query returned malformed JSON for " + identity) from error
        if not isinstance(rows, list) or len(rows) > 1:
            raise RuntimeError("Condor query cardinality is uncertain for " + identity)
        if rows:
            row = rows[0]
            if (not isinstance(row, dict) or
                    type(row.get("ClusterId")) is not int or row["ClusterId"] != cluster_id or
                    type(row.get("ProcId")) is not int or row["ProcId"] != proc_id or
                    type(row.get("JobStatus")) is not int):
                raise RuntimeError("Condor query identity differs from " + identity)
            return row
    raise RuntimeError("Condor outcome unknown for " + identity)


def run_worker(payload_path, member_index, cluster_id, proc_id):
    """Execute one frozen member without writing authoritative campaign state."""
    payload = read_json(payload_path)
    provenance = validate_direct_grid_provenance(read_json(payload["manifest"]))
    manifest_reference = file_identity(payload["manifest"])
    record = read_json(payload["record"])
    validate_direct_attempt_record(provenance, record, manifest_reference["sha256"])
    if (record["attempt_id"] != payload["attempt_id"] or
            record["member_set"] != provenance["expected_members"] or
            record["created_from"]["path"] != manifest_reference["resolved_path"]):
        raise RuntimeError("Condor worker payload differs from reserved attempt")
    index = int(member_index)
    if (index < 0 or index >= len(provenance["expected_members"]) or
            not str(cluster_id).isdigit() or not str(proc_id).isdigit() or index != int(proc_id)):
        raise RuntimeError("Condor worker member or scheduler identity is invalid")
    member = provenance["expected_members"][index]
    argv = provenance["command_argvs"][index]
    work_directory = Path(payload_path).parent / "member_{:04d}".format(index)
    if work_directory.resolve() != (Path(provenance["scheduler_directory"]) /
                                    record["attempt_id"] / work_directory.name).resolve():
        raise RuntimeError("Condor worker directory differs from planned path")
    runtime = payload["runtime"]
    start_path = work_directory / "worker_start.json"
    result_path = work_directory / "worker_result.json"
    write_json_exclusive(start_path, {"attempt_id": record["attempt_id"],
        "job_set_id": provenance["job_set_id"], "member": member, "command_argv": argv,
        "runtime": runtime, "scheduler_id": [int(cluster_id), int(proc_id)],
        "started_utc": datetime.now(timezone.utc).isoformat()})
    outcome = {"attempt_id": record["attempt_id"], "job_set_id": provenance["job_set_id"],
               "member": member, "command_argv": argv, "runtime": runtime,
               "scheduler_id": [int(cluster_id), int(proc_id)],
               "stdout": str(work_directory / "stdout.txt"),
               "stderr": str(work_directory / "stderr.txt"),
               "exit_code": None, "status": "launch_failed", "finished_utc": None}
    script = ("set -euo pipefail -C\n"
        "export SCRAM_ARCH=\"$2\"\nset +u\nsource \"$1\"\ncd \"$3\"\ncmsenv\nset -u\n"
        "ulimit -s unlimited\nulimit -c 0\nhash -r\n"
        "resolved_combine=\"$(command -v combine || true)\"\n"
        "printf '%s\\n' \"SCRAM_ARCH=$SCRAM_ARCH\" \"CMSSW_BASE=${CMSSW_BASE-}\" \"combine_executable=$resolved_combine\" \"stack=$(ulimit -s)\" > \"$5\"\n"
        "if [[ -z \"${CMSSW_BASE-}\" || \"$CMSSW_BASE\" != /* || ! -d \"$CMSSW_BASE/src\" || \"$(cd -- \"$CMSSW_BASE/src\" && pwd -P)\" != \"$(pwd -P)\" || \"$SCRAM_ARCH\" != \"$2\" || \"$resolved_combine\" != /* || ! -x \"$resolved_combine\" || ! -x \"$CMSSW_BASE/bin/$2/combine\" || \"$(readlink -f -- \"$resolved_combine\")\" != \"$(readlink -f -- \"$CMSSW_BASE/bin/$2/combine\")\" ]]; then\n"
        "  printf 'CMSSW runtime does not match configured release, architecture, or Combine executable\\n' >&2\n  exit 1\nfi\n"
        "cd \"$4\"\nshift 5\nexec \"$@\"\n")
    worker_command = ["/bin/bash", "--noprofile", "--norc", "-c", script,
        "eftfit_condor_worker", runtime["cmsset_default"]["resolved_path"],
        runtime["scram_arch"], runtime["release_src"], str(work_directory),
        str(work_directory / "runtime_resolution.txt"), *argv]
    environment = {"PATH": "/usr/bin:/bin"}
    for key in ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP"):
        if key in os.environ:
            environment[key] = os.environ[key]
    try:
        if file_identity(runtime["cmsset_default"]["resolved_path"]) != runtime["cmsset_default"]:
            raise ValueError("Condor CMSSW setup changed after dispatch intent")
        if file_identity(provenance["workspace_identity"]["resolved_path"])["sha256"] != provenance["workspace_identity"]["sha256"]:
            raise ValueError("Condor workspace changed after planning")
        with (work_directory / "stdout.txt").open("x") as stdout, (work_directory / "stderr.txt").open("x") as stderr:
            completed = subprocess.run(worker_command, cwd=work_directory, env=environment,
                                       stdout=stdout, stderr=stderr, check=False)
        outcome["exit_code"] = completed.returncode
        outcome["status"] = "completed" if completed.returncode == 0 else "nonzero"
        resolution_path = work_directory / "runtime_resolution.txt"
        if resolution_path.is_file():
            try:
                resolved = dict(line.split("=", 1) for line in resolution_path.read_text().splitlines())
            except ValueError:
                resolved = {}
            if (set(resolved) != {"SCRAM_ARCH", "CMSSW_BASE", "combine_executable", "stack"} or
                    resolved["SCRAM_ARCH"] != runtime["scram_arch"] or resolved["stack"] != "unlimited" or
                    not Path(resolved["CMSSW_BASE"]).is_absolute() or
                    not Path(resolved["combine_executable"]).is_absolute() or
                    not os.access(resolved["combine_executable"], os.X_OK) or
                    (Path(resolved["CMSSW_BASE"]) / "src").resolve() != Path(runtime["release_src"]).resolve() or
                    Path(resolved["combine_executable"]).resolve() !=
                    (Path(resolved["CMSSW_BASE"]) / "bin" / runtime["scram_arch"] / "combine").resolve()):
                outcome["status"] = "runtime_unresolved"
            else:
                outcome["resolved_runtime"] = resolved
        elif completed.returncode == 0:
            outcome["status"] = "runtime_unresolved"
        if outcome["status"] == "completed":
            output = work_directory / member["file_name"]
            destination = Path(provenance["execution_directory"]) / member["file_name"]
            if output.is_symlink() or (output.exists() and not output.is_file()):
                outcome["status"] = "unsafe_output"
            elif output.is_file():
                try:
                    os.link(output, destination, follow_symlinks=False)
                except FileExistsError:
                    outcome["status"] = "collision"
                except OSError as error:
                    outcome["status"] = "publication_failed"
                    outcome["error"] = str(error)
    except (OSError, ValueError) as error:
        outcome["error"] = str(error)
    outcome["finished_utc"] = datetime.now(timezone.utc).isoformat()
    write_json_exclusive(result_path, outcome)
    return 0 if outcome["status"] == "completed" else 1


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[1] != "worker":
        raise SystemExit("usage: campaign_condor.py worker PAYLOAD MEMBER CLUSTER PROC")
    raise SystemExit(run_worker(*sys.argv[2:]))

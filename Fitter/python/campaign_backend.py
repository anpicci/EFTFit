"""No-execution local and Condor renderers for one 009A direct-grid plan."""

import os
import platform
import re
import shlex
import sys
from pathlib import Path

from .execution_provenance import (build_direct_grid_provenance, sha256_file,
                                   validate_direct_grid_provenance)
from .wc_fit_config import build_configured_grid_scan_command
from .campaign_spec import digest, fit_config, planned_points


def runtime_identity():
    return {"python_executable": sys.executable, "python_version": platform.python_version(),
            "platform": platform.platform(), "execution_environment": "not_resolved_no_submit",
            "source_sha256": {name: sha256_file(Path(__file__).with_name(name)) for name in (
                "campaign_backend.py", "campaign_spec.py", "campaign_state.py", "campaign_manager.py",
                "execution_provenance.py", "wc_fit_config.py", "inference_recipe.py", "scan_1d.py")}}


def grid_argv(spec, point, strategy):
    args = build_configured_grid_scan_command(fit_config(spec), spec["workspace"]["resolved_path"],
        name="", freeze=point["mode"] == "frozen", scan_params=[point["wc"]],
        points=spec["grid"]["points"], other=["--dataset", spec["dataset"]["name"],
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
            scan_name = ".campaign_" + digest({"campaign_id": request["campaign_id"],
                "generation_id": request["generation_id"], "scan_id": scan_id,
                "spec_digest": request["spec_digest"]})
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
    capabilities = {"plan": True, "dispatch": False, "collect": False}


def select_backend(name, backends=None):
    providers = {"condor": condor_plan_backend(), "local": local_plan_backend()} if backends is None else backends
    if name is None:
        raise ValueError("select --backend explicitly or set backend in the campaign YAML")
    if name not in providers:
        raise ValueError("backend {!r} is unavailable; no fallback (shipped: condor and local, plan only)".format(name))
    provider = providers[name]
    if provider.name != name or not provider.capabilities.get("plan"):
        raise ValueError("provider identity or plan capability mismatch")
    return provider

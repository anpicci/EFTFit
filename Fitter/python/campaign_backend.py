"""Provider seam. Only Condor no-submit planning is shipped.

Providers own plan/dispatch/collect and execution receipts. Campaigns own no
scheduler. Test providers are injected by callers, never discovered from YAML.
"""

import platform
import sys
from pathlib import Path

from .execution_provenance import compute_job_set_id, identity_semantic_argv, sha256_file
from .wc_fit_config import build_configured_grid_scan_command
from .campaign_spec import digest, fit_config


def runtime_identity():
    return {"python_executable": sys.executable, "python_version": platform.python_version(),
            "platform": platform.platform(), "execution_environment": "not_resolved_no_submit",
            "source_sha256": {name: sha256_file(Path(__file__).with_name(name)) for name in (
                "campaign_backend.py", "campaign_spec.py", "campaign_state.py", "campaign_manager.py",
                "execution_provenance.py", "wc_fit_config.py", "inference_recipe.py", "scan_1d.py")}}


def grid_argv(spec, point, first, last, strategy):
    args = build_configured_grid_scan_command(fit_config(spec), spec["workspace"]["resolved_path"],
        name="", freeze=point["mode"] == "frozen", scan_params=[point["wc"]],
        points=spec["grid"]["points"], other=["--dataset", spec["dataset"]["name"],
        "--alignEdges", "1" if spec["grid"]["align_edges"] else "0",
        "--firstPoint", str(first), "--lastPoint", str(last)])
    if strategy == "minimizer_strategy_1":
        args[args.index("--cminDefaultMinimizerStrategy=0")] = "--cminDefaultMinimizerStrategy=1"
    return args


class condor_plan_backend:
    name = "condor"
    capabilities = {"plan": True, "dispatch": False, "collect": False}

    def plan(self, request, spec):
        jobs = []
        scans = {}
        for point in request["points"]:
            scans.setdefault(point["scan_id"], []).append(point)
        for scan_points in scans.values():
            # Each contiguous selected range is a distinct execution job basis.
            groups = []
            for point in scan_points:
                if not groups or point["point_index"] != groups[-1][-1]["point_index"] + 1:
                    groups.append([])
                groups[-1].append(point)
            for group in groups:
                first, last = group[0]["point_index"], group[-1]["point_index"]
                point = group[0]
                scan_name = ".campaign_" + digest({"campaign_id": request["campaign_id"],
                    "generation_id": request["generation_id"], "attempt_id": request["attempt_id"],
                    "scan_id": point["scan_id"], "first_point": first})
                argv = grid_argv(spec, point, first, last, request["strategy"])
                argv += ["--job-mode", "condor", "--split-points", str(spec["grid"]["points"]), "--dry-run"]
                job_set_id = compute_job_set_id("condor", scan_name, identity_semantic_argv(argv), spec["workspace"]["sha256"])
                output_label = scan_name + ".job_" + job_set_id
                argv += ["-n", output_label, "--task-name", scan_name.replace(".", "") + "_" + job_set_id]
                jobs.append({"job_set_id": job_set_id, "scan_name": scan_name, "argv": argv,
                             "scan_id": point["scan_id"], "point_indices": [p["point_index"] for p in group],
                             "expected_execution_attempt_id": "attempt_0001"})
        return {"requested_backend": request["requested_backend"], "actual_backend": self.name,
                "runtime": runtime_identity(), "jobs": jobs, "capabilities": self.capabilities}


def select_backend(name, backends=None):
    providers = {"condor": condor_plan_backend()} if backends is None else backends
    if name is None:
        raise ValueError("select --backend explicitly or set backend in the campaign YAML")
    if name not in providers:
        raise ValueError("backend {!r} is unavailable; no fallback (shipped: condor, plan only)".format(name))
    provider = providers[name]
    if provider.name != name or not provider.capabilities.get("plan"):
        raise ValueError("provider identity or plan capability mismatch")
    return provider

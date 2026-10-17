#!/usr/bin/env python3
"""Canonical campaign CLI; running this script needs no CMSSW setup."""

import argparse
import json
from pathlib import Path
import sys

# The source tree's python directory is also a package; avoid CMSSW bootstrap.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from python import campaign_manager as manager


def main(argv=None, *, backends=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="campaign storage directory")
    parser.add_argument("--json", action="store_true", help="emit the complete machine-readable result")
    commands = parser.add_subparsers(dest="operation", required=True)
    create = commands.add_parser("create", help="freeze one YAML; no submission")
    create.add_argument("spec")
    create.add_argument("--dry-run", action="store_true")
    create.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    for operation in ("status", "restart", "clone", "prepare-reference", "plan", "retry", "rescue", "dispatch", "collect"):
        command = commands.add_parser(operation)
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        command.add_argument("campaign_id")
        if operation != "status":
            command.add_argument("--dry-run", action="store_true")
        if operation == "restart":
            command.add_argument("--expected-revision", type=int)
        if operation in ("plan", "retry", "rescue"):
            command.add_argument("--backend", help="explicit provider override; never changes frozen spec")
            command.add_argument("--scan-id", help="optional WC:mode scope; points selected automatically")
        if operation == "prepare-reference":
            command.add_argument("--scan-id", required=True, help="logical WC:mode scan to prepare")
            command.add_argument("--backend", help="provider used for subsequent members")
        if operation == "rescue":
            command.add_argument("--strategy", required=True)
        if operation in ("dispatch", "collect"):
            command.add_argument("attempt_id")
    options = vars(parser.parse_args(argv))
    operation = options.pop("operation")
    as_json = options.pop("json")
    try:
        if operation in ("plan", "retry", "rescue"):
            result = manager.plan(**options, operation=operation, backends=backends)
        elif operation in ("dispatch", "collect"):
            result = getattr(manager, operation)(**options, backends=backends)
        elif operation == "create":
            options["spec_path"] = options.pop("spec")
            result = manager.create(**options)
        elif operation == "prepare-reference":
            result = manager.prepare_reference(**options, backends=backends)
        else:
            result = getattr(manager, operation)(**options)
        if as_json:
            print(json.dumps(result, sort_keys=True, allow_nan=False))
        else:
            summary = ["operation: {}".format(operation)]
            campaign_id = result.get("campaign_id", result.get("request", {}).get("campaign_id",
                                            options.get("campaign_id")))
            if campaign_id:
                summary.append("campaign: {}".format(campaign_id))
            request = result.get("request", {})
            plan = result.get("plan", {})
            if request:
                summary.append("selected points: {}".format(len(request.get("points", []))))
                summary.append("attempt: {}".format(request.get("attempt_id", "none")))
                for job in plan.get("jobs", []):
                    provenance = job.get("execution_provenance", {})
                    member_count = len(job.get("expected_members", []))
                    summary.append("{}: {} points, {} per member, {} {}, reference {}".format(
                        job["scan_id"], len(job["point_indices"]),
                        provenance.get("points_per_member", "legacy all"),
                        member_count, "member" if member_count == 1 else "members",
                        "ready" if provenance.get("shared_reference") else "independent"))
            elif "points" in result:
                summary.append("points: {}".format(len(result["points"])))
                summary.append("attempts: {}".format(len(result.get("attempts", []))))
            if "reference" in result:
                summary.append("reference: {}".format(result.get("state", "ready")))
            for scan_id, reference in sorted(result.get("references", {}).items()):
                summary.append("reference {}: {}".format(scan_id, reference["state"]))
            summary.append("next action: {}".format({"create": "prepare-reference or plan",
                "prepare-reference": "plan", "plan": "dispatch after review",
                "retry": "dispatch after review", "rescue": "dispatch after review",
                "dispatch": "collect after completion", "collect": "inspect status and retry eligible points"}.get(operation, "inspect status")))
            print("\n".join(summary))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(json.dumps({"operation": operation, "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

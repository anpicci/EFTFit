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
    commands = parser.add_subparsers(dest="operation", required=True)
    create = commands.add_parser("create", help="freeze one YAML; no submission")
    create.add_argument("spec")
    create.add_argument("--dry-run", action="store_true")
    for operation in ("status", "restart", "clone", "plan", "retry", "rescue", "dispatch", "collect"):
        command = commands.add_parser(operation)
        command.add_argument("campaign_id")
        if operation != "status":
            command.add_argument("--dry-run", action="store_true")
        if operation == "restart":
            command.add_argument("--expected-revision", type=int)
        if operation in ("plan", "retry", "rescue"):
            command.add_argument("--backend", help="explicit provider override; never changes frozen spec")
            command.add_argument("--scan-id", help="optional WC:mode scope; points selected automatically")
        if operation == "rescue":
            command.add_argument("--strategy", required=True)
        if operation in ("dispatch", "collect"):
            command.add_argument("attempt_id")
    options = vars(parser.parse_args(argv))
    operation = options.pop("operation")
    try:
        if operation in ("plan", "retry", "rescue"):
            result = manager.plan(**options, operation=operation, backends=backends)
        elif operation in ("dispatch", "collect"):
            result = getattr(manager, operation)(**options, backends=backends)
        elif operation == "create":
            options["spec_path"] = options.pop("spec")
            result = manager.create(**options)
        else:
            result = getattr(manager, operation)(**options)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(json.dumps({"operation": operation, "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

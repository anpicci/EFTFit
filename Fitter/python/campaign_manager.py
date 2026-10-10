"""Campaign lifecycle and scientific receipts, independent of scheduling."""

from pathlib import Path
import math

from .campaign_backend import select_backend
from .campaign_spec import digest, load_spec, planned_points
from .campaign_state import campaign_store, new_id, checked_id
from .execution_provenance import (file_identity, read_json, sha256_file,
                                   validate_direct_attempt_record, validate_execution_identity,
                                   validate_workspace_identity)
from .scan_1d import read_scan


def point_key(point):
    return point["scan_id"], point["point_index"]


def planning_eligibility(operation, points, attempts, receipts, generation_id):
    """Derive work from active-generation execution and scientific evidence."""
    relevant_attempts = {attempt_id: attempt for attempt_id, attempt in attempts.items()
                         if attempt["generation_id"] == generation_id}
    collected = {receipt["attempt_id"] for receipt in receipts
                 if receipt["generation_id"] == generation_id}
    history = {}
    for attempt_id, attempt in relevant_attempts.items():
        for point in attempt["points"]:
            history.setdefault(point_key(point), []).append((attempt_id, attempt))
    by_point = {}
    for receipt in receipts:
        if receipt["generation_id"] == generation_id:
            by_point.setdefault(point_key(receipt), []).append(receipt)

    selected, deferred = [], []
    for point in points:
        key = point_key(point)
        attempts_for_point = history.get(key, [])
        point_receipts = by_point.get(key, [])
        if point["canonical"] is not None or any(r["status"] == "accepted" for r in point_receipts):
            reason = ("accepted_valid_candidate" if point["status"] == "accepted"
                      else "canonical_reference_damaged")
        elif any(a["dispatch"] and attempt_id not in collected for attempt_id, a in attempts_for_point):
            reason = "not_yet_recoverable_dispatch_intent"
        elif any(r["status"] == "unresolved" and r["reason"] == "PROMOTION_UNQUALIFIED_REFERENCE"
                 for r in point_receipts):
            reason = "promotion_unqualified_reference"
        elif operation == "plan":
            reason = ("initial_unattempted" if not point_receipts and
                      not any(a["dispatch"] for _, a in attempts_for_point)
                      else "already_attempted")
        elif not point_receipts:
            reason = "not_yet_recoverable_plan_only" if attempts_for_point else "unattempted"
        elif operation == "retry":
            reason = "terminal_result_requires_repeat" if point["status"] in ("missing", "invalid", "unresolved") else "not_yet_recoverable"
        else:
            reason = ("numerical_failure_strategy_applicable"
                      if any(r["status"] == "invalid" and r["reason"] == "invalid evaluation point (scan_1d)"
                             for r in point_receipts) and point["status"] == "invalid"
                      and point["reason"] == "invalid evaluation point (scan_1d)"
                      else "no_confirmed_numerical_failure")
        if reason in ("initial_unattempted", "terminal_result_requires_repeat", "numerical_failure_strategy_applicable"):
            selected.append(point)
        else:
            deferred.append({"scan_id": point["scan_id"], "point_index": point["point_index"],
                             "reason": reason, "status": point["status"]})
    return selected, deferred


def verify_reference(reference):
    if sha256_file(reference["resolved_path"]) != reference["sha256"]:
        raise ValueError("referenced artifact changed: " + reference["resolved_path"])


def current_state(snapshot):
    generations, attempts, receipts = [], {}, []
    for event in snapshot["events"]:
        payload = event["payload"]
        if event["kind"] == "generation":
            if payload["previous_generation_id"] != (generations[-1]["generation_id"] if generations else None):
                raise ValueError("generation chain mismatch")
            checked_id(payload["generation_id"])
            if any(g["generation_id"] == payload["generation_id"] for g in generations):
                raise ValueError("duplicate generation")
            generations.append(payload)
        elif event["kind"] == "attempt":
            attempt_id = checked_id(payload["attempt_id"])
            if attempt_id in attempts or not generations or payload["generation_id"] != generations[-1]["generation_id"]:
                raise ValueError("attempt identity/generation mismatch")
            attempts[attempt_id] = dict(payload, dispatch=None)
        elif event["kind"] == "dispatch":
            attempt = attempts[payload["attempt_id"]]
            if attempt["dispatch"] is not None:
                raise ValueError("duplicate dispatch")
            attempt["dispatch"] = payload
        elif event["kind"] == "validation_batch":
            receipts.extend(payload["receipts"])
        else:
            raise ValueError("unknown campaign event")
    active = generations[-1] if generations else None
    return active, generations, attempts, receipts


def status(root, campaign_id):
    snapshot = campaign_store(root, campaign_id).read()
    result = {"campaign_id": campaign_id, "revision": snapshot["revision"],
              "incomplete": snapshot["incomplete"], "active_generation_id": None,
              "points": [], "attempts": [], "generations": []}
    if "campaign" not in snapshot:
        return result
    result["spec_digest"] = snapshot["campaign"]["spec_digest"]
    try:
        active, generations, attempts, receipts = current_state(snapshot)
        result["generations"] = [g["generation_id"] for g in generations]
        if active is None:
            return result
        result["active_generation_id"] = active["generation_id"]
        points = {point_key(p): dict(p, status="planned", reason="no scientific receipt", canonical=None)
                  for p in active["points"]}
        for attempt in attempts.values():
            result["attempts"].append({key: attempt[key] for key in (
                "attempt_id", "generation_id", "requested_backend", "actual_backend", "runtime", "execution_links")})
            if attempt["generation_id"] != active["generation_id"]:
                continue
            for point in attempt["points"]:
                points[point_key(point)].update(status="running" if attempt["dispatch"] else "planned",
                                               reason="dispatched; no scientific receipt" if attempt["dispatch"] else "plan only")
        reference_checks = {}
        for receipt in receipts:
            if receipt["generation_id"] != active["generation_id"]:
                continue
            point = points[(receipt["scan_id"], receipt["point_index"])]
            # Never displace an accepted canonical point with a later attempt.
            if point["canonical"] is not None:
                continue
            state, reason = receipt["status"], receipt["reason"]
            try:
                for reference in receipt["references"]:
                    reference_key = (reference["resolved_path"], reference["sha256"])
                    if reference_key not in reference_checks:
                        try:
                            verify_reference(reference)
                            reference_checks[reference_key] = None
                        except (OSError, ValueError) as error:
                            reference_checks[reference_key] = error
                    if reference_checks[reference_key] is not None:
                        raise reference_checks[reference_key]
            except (OSError, ValueError) as error:
                state, reason = "missing" if isinstance(error, FileNotFoundError) else "invalid", str(error)
            point.update(status=state, reason=reason)
            if state == "accepted":
                point["canonical"] = {"attempt_id": receipt["attempt_id"], "execution_link": receipt["execution_link"]}
        collected = {r["attempt_id"] for r in receipts}
        for attempt in attempts.values():
            if attempt["generation_id"] == active["generation_id"] and attempt["dispatch"] and attempt["attempt_id"] not in collected:
                for selected in attempt["points"]:
                    point = points[point_key(selected)]
                    if point["canonical"] is None:
                        point.update(status="running", reason="dispatch intent; collection or interruption investigation required")
        result["points"] = list(points.values())
    except (KeyError, TypeError, ValueError) as error:
        result["incomplete"].append("invalid lifecycle history: " + str(error))
    return result


def require_complete(snapshot):
    if snapshot["incomplete"]:
        raise ValueError("incomplete campaign transaction; inspect status before explicit recovery")


def create(root, spec_path, dry_run=False):
    spec, source = load_spec(spec_path)
    points = planned_points(spec)
    if dry_run:
        return {"operation": "create", "dry_run": True, "spec_digest": digest(spec), "points": points}
    store = campaign_store(root, new_id())
    store.create(spec, source, points)
    return status(root, store.campaign_id)


def clone(root, campaign_id, dry_run=False):
    snapshot = campaign_store(root, campaign_id).read()
    # Clone uses immutable campaign spec even when generation/output state is incomplete.
    if "campaign" not in snapshot:
        raise ValueError("source campaign specification is unreadable")
    campaign = snapshot["campaign"]
    if dry_run:
        return {"operation": "clone", "dry_run": True, "parent_campaign_id": campaign_id,
                "spec_digest": campaign["spec_digest"]}
    store = campaign_store(root, new_id())
    store.create(campaign["effective_spec"], campaign["source_yaml"],
                 planned_points(campaign["effective_spec"]), parent=campaign_id)
    return status(root, store.campaign_id)


def restart(root, campaign_id, dry_run=False, expected_revision=None):
    store = campaign_store(root, campaign_id)
    def transition():
        snapshot = store.read()
        require_complete(snapshot)
        if expected_revision is not None and snapshot["revision"] != expected_revision:
            raise ValueError("stale revision; reread status")
        active, _, _, _ = current_state(snapshot)
        if dry_run:
            return {"operation": "restart", "dry_run": True, "retire_generation_id": active["generation_id"]}
        store.append(snapshot, "generation", {"generation_id": new_id(),
            "previous_generation_id": active["generation_id"], "points": active["points"]})
        return status(root, campaign_id)
    if dry_run:
        return transition()
    with store.locked():
        return transition()


def plan(root, campaign_id, operation="plan", backend=None, strategy=None, dry_run=False,
         backends=None, scan_id=None):
    if operation not in ("plan", "retry", "rescue"):
        raise ValueError("unknown planning operation")
    if operation == "rescue":
        if strategy != "minimizer_strategy_1":
            raise ValueError("rescue requires validated strategy minimizer_strategy_1 (argv contract only; scientific qualification pending)")
    elif strategy is not None:
        raise ValueError("a numerical strategy requires rescue")
    store = campaign_store(root, campaign_id)
    def transition():
        snapshot = store.read()
        require_complete(snapshot)
        observed = status(root, campaign_id)
        if observed["incomplete"]:
            raise ValueError("incomplete lifecycle history")
        spec = snapshot["campaign"]["effective_spec"]
        provider = select_backend(backend if backend is not None else spec["default_backend"], backends)
        if scan_id is not None and scan_id not in {p["scan_id"] for p in observed["points"]}:
            raise ValueError("unknown logical scan_id")
        active, _, attempts, receipts = current_state(snapshot)
        scoped = [p for p in observed["points"] if scan_id is None or p["scan_id"] == scan_id]
        selected, deferred = planning_eligibility(operation, scoped, attempts, receipts,
                                                  active["generation_id"])
        points = [{key: p[key] for key in ("scan_id", "wc", "mode", "point_index", "coordinate")} for p in selected]
        if not points:
            return {"operation": operation, "status": "no_eligible_points", "dry_run": dry_run,
                    "campaign_id": campaign_id, "generation_id": active["generation_id"],
                    "not_yet_recoverable": deferred}
        # Input identities are needed only by execution planning, never status/clone/restart.
        verify_reference(spec["package"])
        validate_workspace_identity(spec["workspace"])
        request = {"campaign_id": campaign_id, "generation_id": observed["active_generation_id"],
                   "attempt_id": "0" * 32 if dry_run else new_id(), "spec_digest": snapshot["campaign"]["spec_digest"],
                   "execution_root": str((store.path / "execution").resolve()),
                   "operation": operation, "requested_backend": provider.name, "points": points,
                   "strategy": strategy, "numerical_difference": ({"--cminDefaultMinimizerStrategy": {"before": 0, "after": 1}} if strategy else {})}
        mapped = provider.plan(request, spec)
        if mapped["actual_backend"] != provider.name or mapped["requested_backend"] != provider.name:
            raise ValueError("provider substitution is forbidden")
        result = {"request": request, "plan": mapped, "dry_run": dry_run}
        if not dry_run and points:
            links = [{key: job[key] for key in ("job_set_id", "scan_name", "scan_id", "point_indices", "expected_execution_attempt_id")} for job in mapped["jobs"]]
            store.append(snapshot, "attempt", dict(request, actual_backend=mapped["actual_backend"],
                         runtime=mapped["runtime"], execution_links=links))
        return result
    if dry_run:
        return transition()
    with store.locked():
        return transition()


def active_attempt(snapshot, attempt_id):
    active, _, attempts, _ = current_state(snapshot)
    attempt = attempts[checked_id(attempt_id)]
    if attempt["generation_id"] != active["generation_id"]:
        raise ValueError("attempt belongs to a retired generation")
    return attempt


def dispatch(root, campaign_id, attempt_id, dry_run=False, backends=None):
    store = campaign_store(root, campaign_id)
    def transition():
        snapshot = store.read()
        require_complete(snapshot)
        attempt = active_attempt(snapshot, attempt_id)
        if attempt["dispatch"]:
            raise ValueError("attempt already dispatched; no implicit resubmission")
        observed = status(root, campaign_id)
        selected_keys = {point_key(p) for p in attempt["points"]}
        if any(point_key(p) in selected_keys and p["status"] in ("accepted", "running") for p in observed["points"]):
            raise ValueError("stale plan overlaps accepted/running points; make a fresh automatic recovery plan")
        provider = select_backend(attempt["actual_backend"], backends)
        if not provider.capabilities.get("dispatch"):
            raise ValueError("backend dispatch is not qualified; campaign supports no-submit plans only")
        if dry_run:
            return {"operation": "dispatch", "dry_run": True, "attempt_id": attempt_id}
        spec = snapshot["campaign"]["effective_spec"]
        verify_reference(spec["package"])
        validate_workspace_identity(spec["workspace"])
        # Write intent BEFORE calling a provider: interruption is never resubmitted.
        snapshot = store.append(snapshot, "dispatch", {"attempt_id": attempt_id,
                              "runtime": provider.runtime(), "state": "intent"})
        provider.dispatch(attempt, spec, store.path / "execution" / attempt_id)
        return status(root, campaign_id)
    if dry_run:
        return transition()
    with store.locked():
        return transition()


def validate_member(attempt, link, member, spec):
    """Bind an independently read ROOT file to immutable 009A job/attempt/member evidence."""
    references = [member["manifest"], member["attempt_record"]]
    for reference in references:
        verify_reference(reference)
    manifest = validate_execution_identity(read_json(member["manifest"]["resolved_path"]))
    record = read_json(member["attempt_record"]["resolved_path"])
    if manifest["job_set_id"] != link["job_set_id"] or manifest["scan_name"] != link["scan_name"] or manifest["workspace_identity"]["sha256"] != spec["workspace"]["sha256"]:
        raise ValueError("009A job/workspace linkage mismatch")
    if record["job_set_id"] != link["job_set_id"] or record["attempt_id"] != link["expected_execution_attempt_id"] or record["created_from"]["sha256"] != member["manifest"]["sha256"] or Path(record["created_from"]["path"]).resolve() != Path(member["manifest"]["resolved_path"]).resolve():
        raise ValueError("009A attempt linkage mismatch")
    if manifest.get("schema") == 4:
        validate_direct_attempt_record(manifest, record, member["manifest"]["sha256"])
        if (link["point_indices"] != manifest["selected_indices"] or
                manifest["execution_root"] != attempt["execution_root"]):
            raise ValueError("direct-grid selected-member linkage mismatch")
    owned = member["member"]
    if owned not in manifest["expected_members"] or owned not in record["member_set"]:
        raise ValueError("009A member ownership mismatch")
    if any(not owned["first_point"] <= index <= owned["last_point"] for index in member["point_indices"]):
        raise ValueError("point index outside execution member")
    if manifest.get("schema") == 4 and member["point_indices"] != list(range(
            owned["first_point"], owned["last_point"] + 1)):
        raise ValueError("direct-grid member mapping is incomplete or reordered")
    path = Path(member["path"]).resolve()
    execution_path = Path(manifest["execution_directory"])
    if not execution_path.is_absolute():
        # 009A manifests normally live in their execution directory.
        execution_path = Path(member["manifest"]["resolved_path"]).parent
    if path != execution_path.resolve() / owned["file_name"]:
        raise ValueError("result is not the exact identity-owned member path")
    path.stat()  # Preserve missing-member classification from FileNotFoundError.
    before = file_identity(path)
    point = next(p for p in attempt["points"] if p["scan_id"] == link["scan_id"])
    scan = read_scan(path, point["wc"], failed_point_policy="mask", family="scan_1d_" + point["mode"])
    if file_identity(path) != before:
        raise ValueError("result changed during scientific validation")
    for reference in references:
        verify_reference(reference)
    return scan, references + [before]


def collect(root, campaign_id, attempt_id, dry_run=False, backends=None):
    store = campaign_store(root, campaign_id)
    def transition():
        snapshot = store.read()
        require_complete(snapshot)
        attempt = active_attempt(snapshot, attempt_id)
        provider = select_backend(attempt["actual_backend"], backends)
        if not provider.capabilities.get("collect"):
            raise ValueError("backend collection is not qualified")
        if not attempt["dispatch"]:
            raise ValueError("attempt has not been dispatched")
        spec = snapshot["campaign"]["effective_spec"]
        # collect() must be a read-only provider operation, including during dry-run.
        members = provider.collect(attempt, store.path / "execution" / attempt_id)
        _, _, _, prior = current_state(snapshot)
        if any(r["attempt_id"] == attempt_id for r in prior):
            raise ValueError("attempt already collected; recovery creates a new attempt")
        anchors = {}
        for receipt in prior:
            if receipt["generation_id"] == attempt["generation_id"] and receipt["status"] == "accepted":
                anchors.setdefault(receipt["scan_id"], receipt["reference_group"])
        by_key = {}
        for member in members:
            link = next((item for item in attempt["execution_links"] if item["job_set_id"] == member["job_set_id"]), None)
            if link is None:
                raise ValueError("foreign execution job")
            for index in member["point_indices"]:
                key = (link["scan_id"], index)
                if index not in link["point_indices"] or key in by_key:
                    raise ValueError("duplicate or foreign logical point mapping")
                by_key[key] = (link, member)
        validations = {}
        receipts = []
        for point in attempt["points"]:
            receipt = {"attempt_id": attempt_id, "generation_id": attempt["generation_id"],
                       "scan_id": point["scan_id"], "point_index": point["point_index"],
                       "coordinate": point["coordinate"], "status": "missing", "reason": "member missing",
                       "references": [], "execution_link": None, "reference_group": None}
            mapped = by_key.get(point_key(point))
            if mapped:
                link, member = mapped
                receipt["execution_link"] = {"job_set_id": link["job_set_id"], "attempt_id": link["expected_execution_attempt_id"], "member": member["member"]}
                receipt["references"] = [member["manifest"], member["attempt_record"]]
                cache_key = (link["job_set_id"], member["path"])
                if cache_key not in validations:
                    try:
                        validations[cache_key] = validate_member(attempt, link, member, spec)
                    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
                        validations[cache_key] = error
                validated = validations[cache_key]
                if isinstance(validated, Exception):
                    receipt.update(status="missing" if isinstance(validated, FileNotFoundError) else "invalid", reason=str(validated))
                else:
                    scan, references = validated
                    receipt["references"] = references
                    lower, upper = spec["fit_config"]["wc_ranges"][point["wc"]]
                    spacing = (upper - lower) / spec["grid"]["points"]
                    tolerance = min(spacing / 4, max(abs(lower), abs(upper), 1) * 2e-7)
                    matching = [p for p in scan.grid_points if math.isclose(p.coordinate, point["coordinate"], rel_tol=0, abs_tol=tolerance)]
                    invalid = [p for p in scan.invalid_entries if math.isfinite(p.coordinate) and abs(p.coordinate - point["coordinate"]) <= tolerance]
                    if matching and not scan.unknown_invalid_coordinate and scan.classification == "combine_grid_quantile":
                        group = {"attempt_id": attempt_id, "job_set_id": link["job_set_id"], "member_sha256": references[-1]["sha256"]}
                        receipt["reference_group"] = group
                        if point["scan_id"] in anchors and anchors[point["scan_id"]] != group:
                            receipt.update(status="unresolved", reason="PROMOTION_UNQUALIFIED_REFERENCE")
                        else:
                            anchors.setdefault(point["scan_id"], group)
                            receipt.update(status="accepted", reason="validated grid point; acceptance is not initial-fit convergence or interval qualification")
                    elif invalid:
                        receipt.update(status="invalid", reason="invalid evaluation point (scan_1d)")
                    else:
                        receipt.update(status="unresolved", reason="grid point absent, unclassified, or unlocatable invalid coordinate")
            receipts.append(receipt)
        if not dry_run:
            # One event commits all point receipts, avoiding partial promotion.
            snapshot = store.append(snapshot, "validation_batch", {"receipts": receipts})
        return {"operation": "collect", "dry_run": dry_run, "receipts": receipts}
    if dry_run:
        return transition()
    with store.locked():
        return transition()

"""Generic filesystem identity and scan-result provenance helpers."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


workspace_identity_schema = 1
final_aggregate_provenance_schema = 2
_job_set_id_pattern = re.compile(r"[0-9a-f]{64}")
_workspace_locator_options = {"-d", "--datacard"}
workspace_identity_token = "<workspace_sha256>"


def canonical_json_bytes(value):
    """Serialize identity-bearing data deterministically."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_file(path):
    """Return the SHA-256 digest of one file without loading it all at once."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_identity(path):
    """Return the byte identity of a regular file."""
    resolved_path = os.path.realpath(os.fspath(path))
    if not os.path.isfile(resolved_path):
        raise RuntimeError("Expected a regular file: {}".format(path))
    size_bytes = os.stat(resolved_path).st_size
    return {
        "resolved_path": resolved_path,
        "size_bytes": size_bytes,
        "sha256": sha256_file(resolved_path),
    }


def workspace_identity(requested_path):
    """Bind a caller's workspace path to its exact current bytes."""
    requested = os.fspath(requested_path)
    identity = file_identity(requested)
    return {
        "schema": workspace_identity_schema,
        "requested_path": requested,
        "resolved_path": identity["resolved_path"],
        "size_bytes": identity["size_bytes"],
        "sha256": identity["sha256"],
    }


def workspace_job_identity_basis(identity):
    """Return the sole workspace content-identity field used by a job ID."""
    _validate_workspace_identity_structure(identity)
    return identity["sha256"]


def _validate_workspace_identity_structure(identity):
    required_keys = {
        "schema",
        "requested_path",
        "resolved_path",
        "size_bytes",
        "sha256",
    }
    if not isinstance(identity, dict) or set(identity) != required_keys:
        raise RuntimeError("Workspace identity has an unexpected schema")
    if identity["schema"] != workspace_identity_schema:
        raise RuntimeError("Workspace identity schema is not supported")
    if not isinstance(identity["requested_path"], str) or not identity["requested_path"]:
        raise RuntimeError("Workspace requested_path is invalid")
    if not os.path.isabs(identity["resolved_path"]):
        raise RuntimeError("Workspace resolved_path must be absolute")
    if not isinstance(identity["size_bytes"], int) or identity["size_bytes"] < 0:
        raise RuntimeError("Workspace size_bytes is invalid")
    if not isinstance(identity["sha256"], str) or not _job_set_id_pattern.fullmatch(
        identity["sha256"]
    ):
        raise RuntimeError("Workspace sha256 is invalid")


def validate_workspace_identity(expected_identity):
    """Re-read the runtime workspace locator and require its recorded SHA-256."""
    _validate_workspace_identity_structure(expected_identity)
    observed = workspace_identity(expected_identity["requested_path"])
    if observed["sha256"] != expected_identity["sha256"]:
        raise RuntimeError(
            "Workspace content identity mismatch: expected sha256 {}, observed {}".format(
                expected_identity["sha256"],
                observed["sha256"],
            )
        )
    return observed


def identity_semantic_argv(runtime_semantic_argv):
    """Canonicalize only the local workspace locator out of runtime semantics."""
    if not isinstance(runtime_semantic_argv, list) or not all(
        isinstance(argument, str) for argument in runtime_semantic_argv
    ):
        raise RuntimeError("runtime_semantic_argv is invalid")
    identity_semantic = list(runtime_semantic_argv)
    locator_indices = [
        index
        for index, argument in enumerate(identity_semantic)
        if argument in _workspace_locator_options
    ]
    if len(locator_indices) != 1:
        raise RuntimeError("A workspace locator must occur exactly once")
    locator_index = locator_indices[0]
    if locator_index + 1 >= len(identity_semantic):
        raise RuntimeError("{} requires a value".format(identity_semantic[locator_index]))
    identity_semantic[locator_index + 1] = workspace_identity_token
    return identity_semantic


def normalize_grid_indices(original_points, selected_indices):
    """Return exact original-grid membership and its maximal contiguous ranges."""
    if type(original_points) is not int or original_points < 2:
        raise ValueError("original grid size must be an integer >= 2")
    if not isinstance(selected_indices, (list, tuple)) or not selected_indices:
        raise ValueError("selected grid indices must be nonempty")
    if any(type(index) is not int or index < 0 or index >= original_points
           for index in selected_indices):
        raise ValueError("selected grid index is outside the original grid")
    if len(set(selected_indices)) != len(selected_indices):
        raise ValueError("duplicate selected grid index")
    indices = sorted(selected_indices)
    intervals = []
    for index in indices:
        if not intervals or index != intervals[-1][1] + 1:
            intervals.append([index, index])
        else:
            intervals[-1][1] = index
    return indices, intervals


def job_set_identity_basis(batch, scan_name, identity_semantic_argv, workspace_sha256,
                           original_points=None, selected_indices=None):
    """Build the portable logical job identity basis."""
    if batch not in ("condor", "direct_grid"):
        raise RuntimeError("Job identity batch is not supported")
    validate_scan_name(scan_name)
    if not isinstance(identity_semantic_argv, list) or not all(
        isinstance(argument, str) for argument in identity_semantic_argv
    ):
        raise RuntimeError("identity_semantic_argv is invalid")
    if not isinstance(workspace_sha256, str) or not _job_set_id_pattern.fullmatch(
        workspace_sha256
    ):
        raise RuntimeError("workspace_sha256 is invalid")
    basis = {
        "batch": batch,
        "scan_name": scan_name,
        "identity_semantic_argv": identity_semantic_argv,
        "workspace_sha256": workspace_sha256,
    }
    if batch == "direct_grid":
        indices, _ = normalize_grid_indices(original_points, selected_indices)
        basis.update(original_points=original_points, selected_indices=indices)
    elif original_points is not None or selected_indices is not None:
        raise RuntimeError("Legacy Condor identity cannot take selected indices")
    return basis


def compute_job_set_id(batch, scan_name, identity_semantic_argv, workspace_sha256,
                       original_points=None, selected_indices=None):
    """Return the SHA-256 identifier for one portable logical job basis."""
    basis = job_set_identity_basis(
        batch,
        scan_name,
        identity_semantic_argv,
        workspace_sha256,
        original_points,
        selected_indices,
    )
    return hashlib.sha256(canonical_json_bytes(basis)).hexdigest()


def _single_option(argv, option):
    if argv.count(option) != 1:
        raise ValueError("{} must occur exactly once".format(option))
    index = argv.index(option)
    if index + 1 >= len(argv):
        raise ValueError("{} requires a value".format(option))
    return argv[index + 1]


def build_direct_grid_provenance(scan_name, base_argv, recorded_workspace_identity,
                                 original_points, selected_indices, execution_root):
    """Build one provider-neutral 009A member contract; perform no I/O."""
    validate_scan_name(scan_name)
    indices, intervals = normalize_grid_indices(original_points, selected_indices)
    if not isinstance(base_argv, list) or not all(isinstance(arg, str) for arg in base_argv):
        raise ValueError("direct grid argv must be a list of strings")
    if not base_argv or base_argv[0] != "combine":
        raise ValueError("direct grid command must invoke combine")
    unsupported = ("--gridPoints", "--firstPoint", "--lastPoint", "--split-points",
                   "--job-mode", "--dry-run", "--keepFailures", "-n", "--task-name")
    if any(arg == option or arg.startswith(option + "=")
           for arg in base_argv for option in unsupported):
        raise ValueError("direct grid base argv contains an unsupported or member-specific option")
    if _single_option(base_argv, "--points") != str(original_points):
        raise ValueError("direct grid command changed the original point count")
    if _single_option(base_argv, "-M") != "MultiDimFit" or _single_option(base_argv, "--algo") != "grid":
        raise ValueError("direct grid command requires MultiDimFit grid")
    if os.path.realpath(_single_option(base_argv, "-d")) != recorded_workspace_identity["resolved_path"]:
        raise ValueError("direct grid workspace locator differs from recorded workspace")
    _validate_workspace_identity_structure(recorded_workspace_identity)
    identity_argv = identity_semantic_argv(base_argv)
    identity_basis = job_set_identity_basis("direct_grid", scan_name, identity_argv,
        recorded_workspace_identity["sha256"], original_points, indices)
    job_set_id = compute_job_set_id("direct_grid", scan_name, identity_argv,
                                   recorded_workspace_identity["sha256"], original_points, indices)
    output_label = "{}.job_{}".format(scan_name, job_set_id)
    root = os.path.abspath(os.fspath(execution_root))
    execution_directory = os.path.join(root, "direct{}.job_{}".format(scan_name, job_set_id))
    members = []
    commands = []
    for member_index, (first, last) in enumerate(intervals):
        label = "{}.POINTS.{}.{}".format(output_label, first, last)
        members.append({"member_index": member_index, "first_point": first,
                        "last_point": last,
                        "file_name": "higgsCombine{}.MultiDimFit.mH120.root".format(label)})
        commands.append(base_argv + ["--firstPoint", str(first), "--lastPoint", str(last),
                                     "-n", label])
    aggregate = canonical_aggregate_path(scan_name, job_set_id,
        os.path.join(root, "fit_files"))
    return {"schema": 4, "batch": "direct_grid", "scan_name": scan_name,
            "job_set_id": job_set_id, "workspace_identity": recorded_workspace_identity,
            "runtime_semantic_argv": list(base_argv), "identity_semantic_argv": identity_argv,
            "identity_basis": identity_basis, "original_points": original_points,
            "selected_indices": indices, "output_label": output_label,
            "expected_members": members, "command_argvs": commands,
            "execution_root": root, "execution_directory": execution_directory,
            "attempts_directory": os.path.join(execution_directory, "attempts"),
            "scheduler_directory": os.path.join(execution_directory, "scheduler"),
            "aggregate_output": aggregate,
            "final_provenance": final_provenance_path(aggregate)}


def validate_direct_grid_provenance(provenance, expected_scan_name=None):
    """Regenerate every schema-4 identity, member, command and path."""
    if not isinstance(provenance, dict) or provenance.get("schema") != 4:
        raise RuntimeError("Direct grid provenance schema is not supported")
    if expected_scan_name is not None and provenance.get("scan_name") != expected_scan_name:
        raise RuntimeError("Direct grid scan identity differs")
    try:
        expected = build_direct_grid_provenance(
            provenance["scan_name"], provenance["runtime_semantic_argv"],
            provenance["workspace_identity"], provenance["original_points"],
            provenance["selected_indices"], provenance["execution_root"])
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise RuntimeError("Invalid direct grid provenance: {}".format(error)) from error
    if provenance != expected:
        raise RuntimeError("Direct grid provenance identity, members or commands are inconsistent")
    return provenance


def build_direct_attempt_record(provenance, attempt_id, reason, selected_members,
                                manifest_path, scheduler_artifacts):
    """Bind an attempt-local member subset to exact immutable schema-4 bytes."""
    validate_direct_grid_provenance(provenance)
    if not isinstance(attempt_id, str) or not re.fullmatch(r"campaign_[0-9a-f]{32}", attempt_id):
        raise ValueError("direct-grid attempt_id is invalid")
    if not isinstance(reason, str) or not reason:
        raise ValueError("direct-grid attempt reason is invalid")
    if not isinstance(selected_members, list) or not selected_members:
        raise ValueError("direct-grid attempt has no members")
    expected = {canonical_json_bytes(member) for member in provenance["expected_members"]}
    supplied = [canonical_json_bytes(member) for member in selected_members]
    if len(supplied) != len(set(supplied)) or not set(supplied) <= expected:
        raise ValueError("direct-grid attempt has duplicate or foreign members")
    if not isinstance(scheduler_artifacts, list) or not all(
            isinstance(path, str) and path for path in scheduler_artifacts):
        raise ValueError("direct-grid scheduler artifact references are invalid")
    return {"schema": 2, "attempt_id": attempt_id, "job_set_id": provenance["job_set_id"],
            "reason": reason, "member_set": selected_members,
            "created_from": {"path": os.path.realpath(os.fspath(manifest_path)),
                             "sha256": sha256_file(manifest_path)},
            "scheduler_artifacts": scheduler_artifacts}


def validate_direct_attempt_record(provenance, record, manifest_sha256):
    validate_direct_grid_provenance(provenance)
    if not isinstance(record, dict) or set(record) != {
            "schema", "attempt_id", "job_set_id", "reason", "member_set",
            "created_from", "scheduler_artifacts"} or record.get("schema") != 2:
        raise RuntimeError("Direct-grid attempt record has an unexpected schema")
    if not isinstance(manifest_sha256, str) or not _job_set_id_pattern.fullmatch(manifest_sha256):
        raise RuntimeError("Direct-grid manifest SHA is invalid")
    if not isinstance(record["attempt_id"], str) or not re.fullmatch(
            r"campaign_[0-9a-f]{32}", record["attempt_id"]):
        raise RuntimeError("Direct-grid attempt identity is invalid")
    if record["job_set_id"] != provenance["job_set_id"] or not isinstance(
            record["reason"], str) or not record["reason"]:
        raise RuntimeError("Direct-grid attempt linkage is inconsistent")
    members = record["member_set"]
    if not isinstance(members, list) or not members:
        raise RuntimeError("Direct-grid attempt member_set is invalid")
    expected = {canonical_json_bytes(member) for member in provenance["expected_members"]}
    supplied = [canonical_json_bytes(member) for member in members]
    if len(supplied) != len(set(supplied)) or not set(supplied) <= expected:
        raise RuntimeError("Direct-grid attempt has duplicate or foreign members")
    origin = record["created_from"]
    if not isinstance(origin, dict) or set(origin) != {"path", "sha256"} or not isinstance(
            origin["path"], str) or not origin["path"] or origin["sha256"] != manifest_sha256:
        raise RuntimeError("Direct-grid attempt does not bind immutable manifest")
    if not isinstance(record["scheduler_artifacts"], list) or not all(
            isinstance(path, str) and path for path in record["scheduler_artifacts"]):
        raise RuntimeError("Direct-grid attempt scheduler artifacts are invalid")
    return record


def validate_job_set_id(job_set_id):
    if not isinstance(job_set_id, str) or not _job_set_id_pattern.fullmatch(job_set_id):
        raise RuntimeError("job_set_id must be a full lowercase SHA-256 digest")
    return job_set_id


def validate_scan_name(scan_name):
    if (
        not isinstance(scan_name, str)
        or not scan_name
        or os.path.basename(scan_name) != scan_name
        or "\x00" in scan_name
    ):
        raise RuntimeError("scan_name is not a safe single path component")
    return scan_name


def canonical_aggregate_name(scan_name, job_set_id):
    validate_scan_name(scan_name)
    validate_job_set_id(job_set_id)
    return "higgsCombine{}.job_{}.MultiDimFit.root".format(scan_name, job_set_id)


def canonical_aggregate_path(scan_name, job_set_id, fit_directory="../fit_files"):
    return os.path.join(
        os.fspath(fit_directory), canonical_aggregate_name(scan_name, job_set_id)
    )


def final_provenance_path(canonical_path):
    return os.fspath(canonical_path) + ".provenance.json"


def write_json_exclusive(path, value):
    """Atomically create one JSON file without replacing an existing path."""
    destination = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(destination))
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".{}-".format(os.path.basename(destination)),
        suffix=".tmp",
        dir=parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary_path, destination)
    except FileExistsError:
        raise RuntimeError("Provenance path already exists: {}".format(destination))
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return destination


def read_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def validate_execution_identity(provenance):
    """Validate the generic immutable identity fields of execution provenance."""
    if isinstance(provenance, dict) and provenance.get("schema") == 4:
        return validate_direct_grid_provenance(provenance)
    required = {
        "batch",
        "scan_name",
        "runtime_semantic_argv",
        "identity_semantic_argv",
        "workspace_identity",
    }
    if not isinstance(provenance, dict) or not required.issubset(provenance):
        raise RuntimeError("Execution provenance lacks immutable identity fields")
    if provenance["batch"] != "condor":
        raise RuntimeError("Execution provenance is not for Condor")
    validate_scan_name(provenance["scan_name"])
    if not isinstance(provenance["runtime_semantic_argv"], list):
        raise RuntimeError("Execution runtime_semantic_argv is invalid")
    if provenance["identity_semantic_argv"] != identity_semantic_argv(
        provenance["runtime_semantic_argv"]
    ):
        raise RuntimeError("Execution identity semantic argv is inconsistent")
    _validate_workspace_identity_structure(provenance["workspace_identity"])
    basis = job_set_identity_basis(
        provenance["batch"],
        provenance["scan_name"],
        provenance["identity_semantic_argv"],
        workspace_job_identity_basis(provenance["workspace_identity"]),
    )
    observed_job_set_id = compute_job_set_id(
        provenance["batch"],
        provenance["scan_name"],
        provenance["identity_semantic_argv"],
        workspace_job_identity_basis(provenance["workspace_identity"]),
    )
    if provenance.get("identity_basis") != basis:
        raise RuntimeError("Execution provenance identity basis is inconsistent")
    if provenance.get("job_set_id") != observed_job_set_id:
        raise RuntimeError("Execution provenance job_set_id is inconsistent")
    return provenance


def build_final_provenance(
    execution_provenance,
    canonical_path,
    attempt_history_references,
    publication_state,
):
    """Build byte-bound final aggregate provenance from immutable execution state."""
    validate_execution_identity(execution_provenance)
    canonical = os.path.realpath(os.fspath(canonical_path))
    aggregate_identity = file_identity(canonical)
    result = {
        "schema": final_aggregate_provenance_schema,
        "batch": execution_provenance["batch"],
        "job_set_id": execution_provenance["job_set_id"],
        "scan_name": execution_provenance["scan_name"],
        "workspace_sha256": workspace_job_identity_basis(
            execution_provenance["workspace_identity"]
        ),
        "workspace_provenance": execution_provenance["workspace_identity"],
        "runtime_semantic_argv": execution_provenance["runtime_semantic_argv"],
        "identity_semantic_argv": execution_provenance["identity_semantic_argv"],
        "expected_members": execution_provenance["expected_members"],
        "attempt_history_references": attempt_history_references,
        "canonical_aggregate_name": os.path.basename(canonical),
        "canonical_aggregate_path": canonical,
        "canonical_aggregate_size": aggregate_identity["size_bytes"],
        "canonical_aggregate_sha256": aggregate_identity["sha256"],
        "publication_state": publication_state,
    }
    if execution_provenance.get("schema") == 4:
        result.update(execution_schema=4,
                      original_points=execution_provenance["original_points"],
                      selected_indices=execution_provenance["selected_indices"])
    return result


def validate_final_provenance(provenance_path_value, expected_scan_name=None, expected_job_set_id=None):
    """Validate one final sidecar and its exact canonical ROOT bytes."""
    provenance_path_value = os.fspath(provenance_path_value)
    provenance = read_json(provenance_path_value)
    required_keys = {
        "schema",
        "batch",
        "job_set_id",
        "scan_name",
        "workspace_sha256",
        "workspace_provenance",
        "runtime_semantic_argv",
        "identity_semantic_argv",
        "expected_members",
        "attempt_history_references",
        "canonical_aggregate_name",
        "canonical_aggregate_path",
        "canonical_aggregate_size",
        "canonical_aggregate_sha256",
        "publication_state",
    }
    direct_grid = isinstance(provenance, dict) and provenance.get("execution_schema") == 4
    if direct_grid:
        required_keys |= {"execution_schema", "original_points", "selected_indices"}
    if not isinstance(provenance, dict) or set(provenance) != required_keys:
        raise RuntimeError("Final aggregate provenance has an unexpected schema")
    if provenance["schema"] != final_aggregate_provenance_schema:
        raise RuntimeError("Final aggregate provenance schema is not supported")
    validate_scan_name(provenance["scan_name"])
    validate_job_set_id(provenance["job_set_id"])
    _validate_workspace_identity_structure(provenance["workspace_provenance"])
    if (
        provenance["workspace_sha256"]
        != workspace_job_identity_basis(provenance["workspace_provenance"])
    ):
        raise RuntimeError("Final aggregate workspace provenance is inconsistent")
    if provenance["identity_semantic_argv"] != identity_semantic_argv(
        provenance["runtime_semantic_argv"]
    ):
        raise RuntimeError("Final aggregate identity semantic argv is inconsistent")
    observed_job_set_id = compute_job_set_id(
        provenance["batch"],
        provenance["scan_name"],
        provenance["identity_semantic_argv"],
        provenance["workspace_sha256"],
        provenance["original_points"] if direct_grid else None,
        provenance["selected_indices"] if direct_grid else None,
    )
    if provenance["job_set_id"] != observed_job_set_id:
        raise RuntimeError("Final aggregate job_set_id is inconsistent")
    if expected_scan_name is not None and provenance["scan_name"] != expected_scan_name:
        raise RuntimeError("Final aggregate provenance has the wrong scan_name")
    if expected_job_set_id is not None and provenance["job_set_id"] != expected_job_set_id:
        raise RuntimeError("Final aggregate provenance has the wrong job_set_id")
    expected_name = canonical_aggregate_name(
        provenance["scan_name"], provenance["job_set_id"]
    )
    if provenance["canonical_aggregate_name"] != expected_name:
        raise RuntimeError("Canonical aggregate name identity is inconsistent")
    if os.path.basename(provenance["canonical_aggregate_path"]) != expected_name:
        raise RuntimeError("Canonical aggregate filename identity is inconsistent")
    sidecar_suffix = ".provenance.json"
    if not provenance_path_value.endswith(sidecar_suffix):
        raise RuntimeError("Final aggregate sidecar has an unexpected name")
    canonical_path_value = provenance_path_value[: -len(sidecar_suffix)]
    if os.path.basename(canonical_path_value) != expected_name:
        raise RuntimeError("Final aggregate sidecar is not adjacent to its canonical file")
    observed = file_identity(canonical_path_value)
    expected_identity = {
        "size_bytes": provenance["canonical_aggregate_size"],
        "sha256": provenance["canonical_aggregate_sha256"],
    }
    observed_identity = {
        "size_bytes": observed["size_bytes"],
        "sha256": observed["sha256"],
    }
    if observed_identity != expected_identity:
        raise RuntimeError("Canonical aggregate bytes do not match final provenance")
    return observed["resolved_path"]


def _candidate_job_set_id(filename, scan_name):
    prefix = "higgsCombine{}.job_".format(scan_name)
    suffix = ".MultiDimFit.root"
    if not filename.startswith(prefix) or not filename.endswith(suffix):
        return None
    job_set_id = filename[len(prefix) : -len(suffix)]
    return job_set_id if _job_set_id_pattern.fullmatch(job_set_id) else None


def resolve_canonical_aggregate(scan_name, job_set_id=None, fit_directory="../fit_files"):
    """Resolve exactly one validated identity-bearing aggregate."""
    validate_scan_name(scan_name)
    directory = os.path.abspath(os.fspath(fit_directory))
    if job_set_id is not None:
        validate_job_set_id(job_set_id)
        candidate = os.path.join(directory, canonical_aggregate_name(scan_name, job_set_id))
        if not os.path.isfile(candidate):
            raise FileNotFoundError(
                "Missing canonical aggregate for scan {} and job_set_id {}".format(
                    scan_name, job_set_id
                )
            )
        return validate_final_provenance(
            final_provenance_path(candidate), scan_name, job_set_id
        )

    if not os.path.isdir(directory):
        raise FileNotFoundError("Missing fit result directory: {}".format(directory))
    candidates = []
    for filename in sorted(os.listdir(directory)):
        candidate_job_set_id = _candidate_job_set_id(filename, scan_name)
        if candidate_job_set_id is None:
            continue
        candidate = os.path.join(directory, filename)
        candidates.append(
            validate_final_provenance(
                final_provenance_path(candidate), scan_name, candidate_job_set_id
            )
        )
    if not candidates:
        raise FileNotFoundError(
            "No validated canonical aggregate exists for scan {}".format(scan_name)
        )
    if len(candidates) != 1:
        raise RuntimeError(
            "Multiple validated canonical aggregates exist for scan {}; supply "
            "an explicit job_set_id".format(scan_name)
        )
    return candidates[0]


@contextmanager
def transient_legacy_alias(scan_name, job_set_id, fit_directory="../fit_files"):
    """Materialize one locked, relative, context-bounded stable compatibility link."""
    canonical = resolve_canonical_aggregate(scan_name, job_set_id, fit_directory)
    directory = os.path.abspath(os.fspath(fit_directory))
    alias = os.path.join(directory, "higgsCombine{}.MultiDimFit.root".format(scan_name))
    directory_descriptor = os.open(directory, os.O_RDONLY)
    created_identity = None
    link_text = os.path.relpath(canonical, directory)
    try:
        fcntl.flock(directory_descriptor, fcntl.LOCK_EX)
        if os.path.lexists(alias):
            if os.path.islink(alias):
                raise RuntimeError(
                    "Compatibility alias already exists and was not created by this context"
                )
            raise RuntimeError(
                "Compatibility alias path is occupied by a regular file: {}".format(alias)
            )
        os.symlink(link_text, alias)
        alias_stat = os.lstat(alias)
        created_identity = (alias_stat.st_dev, alias_stat.st_ino)
        yield alias
    finally:
        try:
            if created_identity is not None:
                if not os.path.islink(alias):
                    raise RuntimeError("Owned compatibility alias was replaced during use")
                alias_stat = os.lstat(alias)
                observed_identity = (alias_stat.st_dev, alias_stat.st_ino)
                if observed_identity != created_identity or os.readlink(alias) != link_text:
                    raise RuntimeError("Owned compatibility alias identity changed during use")
                os.unlink(alias)
        finally:
            fcntl.flock(directory_descriptor, fcntl.LOCK_UN)
            os.close(directory_descriptor)


def adopt_legacy_aggregate(
    scan_name,
    job_set_id,
    execution_provenance_path,
    fit_directory="../fit_files",
):
    """Explicitly adopt one provenance-bound legacy regular aggregate."""
    validate_scan_name(scan_name)
    validate_job_set_id(job_set_id)
    execution_provenance = read_json(execution_provenance_path)
    validate_execution_identity(execution_provenance)
    if (
        execution_provenance["scan_name"] != scan_name
        or execution_provenance["job_set_id"] != job_set_id
    ):
        raise RuntimeError("Legacy adoption provenance does not match the requested identity")
    directory = os.path.abspath(os.fspath(fit_directory))
    legacy = os.path.join(directory, "higgsCombine{}.MultiDimFit.root".format(scan_name))
    if os.path.islink(legacy) or not os.path.isfile(legacy):
        raise RuntimeError("Legacy adoption requires a regular stable aggregate")
    legacy_identity = file_identity(legacy)
    canonical = os.path.join(directory, canonical_aggregate_name(scan_name, job_set_id))
    sidecar = final_provenance_path(canonical)
    if os.path.lexists(canonical) or os.path.lexists(sidecar):
        raise RuntimeError("Canonical adoption target already exists")
    legacy_stat = os.stat(legacy)
    os.link(legacy, canonical)
    sidecar_created = False
    try:
        canonical_identity = file_identity(canonical)
        if (
            canonical_identity["size_bytes"] != legacy_identity["size_bytes"]
            or canonical_identity["sha256"] != legacy_identity["sha256"]
        ):
            raise RuntimeError("Canonical adoption changed legacy aggregate bytes")
        attempts = execution_provenance.get("attempt_history_references", [])
        final_provenance = build_final_provenance(
            execution_provenance,
            canonical,
            attempts,
            "adopted_legacy_regular_file",
        )
        write_json_exclusive(sidecar, final_provenance)
        sidecar_created = True
        validate_final_provenance(sidecar, scan_name, job_set_id)
    except Exception:
        if sidecar_created and os.path.isfile(sidecar):
            os.unlink(sidecar)
        if os.path.isfile(canonical):
            canonical_stat = os.stat(canonical)
            if (
                canonical_stat.st_dev == legacy_stat.st_dev
                and canonical_stat.st_ino == legacy_stat.st_ino
            ):
                os.unlink(canonical)
        raise
    os.unlink(legacy)
    return canonical

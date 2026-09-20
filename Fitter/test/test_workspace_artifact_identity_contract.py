import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


FITTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FITTER_DIR / "python"))

import execution_provenance as provenance


def execution_provenance(workspace, scan_name=".artifact.c_one"):
    workspace_record = provenance.workspace_identity(workspace)
    runtime_semantic_argv = [
        "combineTool.py",
        "-d",
        str(workspace),
        "-M",
        "MultiDimFit",
        "--algo",
        "grid",
    ]
    identity_semantic_argv = list(runtime_semantic_argv)
    identity_semantic_argv[2] = "<workspace_sha256>"
    basis = provenance.job_set_identity_basis(
        "condor",
        scan_name,
        identity_semantic_argv,
        workspace_record["sha256"],
    )
    job_set_id = provenance.compute_job_set_id(
        "condor",
        scan_name,
        identity_semantic_argv,
        workspace_record["sha256"],
    )
    return {
        "batch": "condor",
        "scan_name": scan_name,
        "runtime_semantic_argv": runtime_semantic_argv,
        "identity_semantic_argv": identity_semantic_argv,
        "workspace_identity": workspace_record,
        "identity_basis": basis,
        "job_set_id": job_set_id,
        "expected_members": [
            {
                "member_index": 0,
                "first_point": 0,
                "last_point": 9,
                "file_name": "member.root",
            }
        ],
    }


def publish_synthetic(fit_directory, execution, content=b"result"):
    canonical = Path(
        provenance.canonical_aggregate_path(
            execution["scan_name"], execution["job_set_id"], fit_directory
        )
    )
    canonical.write_bytes(content)
    sidecar = provenance.build_final_provenance(
        execution, canonical, [], "synthetic_test_publication"
    )
    provenance.write_json_exclusive(provenance.final_provenance_path(canonical), sidecar)
    return canonical


def test_workspace_identity_is_deterministic_and_content_bound(tmp_path):
    workspace = tmp_path / "workspace.root"
    workspace.write_bytes(b"first")
    first = provenance.workspace_identity(workspace)
    assert provenance.workspace_identity(workspace) == first
    workspace.write_bytes(b"second")
    second = provenance.workspace_identity(workspace)
    assert second["requested_path"] == first["requested_path"]
    assert second["resolved_path"] == first["resolved_path"]
    assert second["sha256"] != first["sha256"]
    with pytest.raises(RuntimeError, match="identity mismatch"):
        provenance.validate_workspace_identity(first)


def test_workspace_job_identity_is_sha_only_and_path_independent(tmp_path):
    first_path = tmp_path / "first" / "workspace.root"
    second_path = tmp_path / "second" / "workspace.root"
    first_path.parent.mkdir()
    second_path.parent.mkdir()
    first_path.write_bytes(b"same workspace bytes")
    second_path.write_bytes(first_path.read_bytes())
    first = execution_provenance(first_path)
    second = execution_provenance(second_path)
    assert first["runtime_semantic_argv"] != second["runtime_semantic_argv"]
    assert first["identity_semantic_argv"] == second["identity_semantic_argv"]
    assert first["job_set_id"] == second["job_set_id"]
    second_path.write_bytes(b"different workspace bytes")
    changed = execution_provenance(second_path)
    assert changed["job_set_id"] != second["job_set_id"]


def test_resolver_zero_one_multiple_and_explicit_identity(tmp_path):
    fit_directory = tmp_path / "fit_files"
    fit_directory.mkdir()
    workspace = tmp_path / "workspace.root"
    workspace.write_bytes(b"workspace")
    first = execution_provenance(workspace)
    with pytest.raises(FileNotFoundError, match="No validated canonical"):
        provenance.resolve_canonical_aggregate(
            first["scan_name"], fit_directory=fit_directory
        )
    first_path = publish_synthetic(fit_directory, first, b"first")
    assert provenance.resolve_canonical_aggregate(
        first["scan_name"], fit_directory=fit_directory
    ) == str(first_path.resolve())
    workspace.write_bytes(b"different workspace")
    second = execution_provenance(workspace)
    second_path = publish_synthetic(fit_directory, second, b"second")
    with pytest.raises(RuntimeError, match="Multiple validated"):
        provenance.resolve_canonical_aggregate(
            first["scan_name"], fit_directory=fit_directory
        )
    assert provenance.resolve_canonical_aggregate(
        first["scan_name"], first["job_set_id"], fit_directory
    ) == str(first_path.resolve())
    assert provenance.resolve_canonical_aggregate(
        second["scan_name"], second["job_set_id"], fit_directory
    ) == str(second_path.resolve())


def test_resolver_rejects_stale_manifest(tmp_path):
    fit_directory = tmp_path / "fit_files"
    fit_directory.mkdir()
    workspace = tmp_path / "workspace.root"
    workspace.write_bytes(b"workspace")
    execution = execution_provenance(workspace)
    canonical = publish_synthetic(fit_directory, execution)
    canonical.write_bytes(b"changed result")
    with pytest.raises(RuntimeError, match="do not match final provenance"):
        provenance.resolve_canonical_aggregate(
            execution["scan_name"], execution["job_set_id"], fit_directory
        )


def test_final_provenance_recomputes_job_identity(tmp_path):
    fit_directory = tmp_path / "fit_files"
    fit_directory.mkdir()
    workspace = tmp_path / "workspace.root"
    workspace.write_bytes(b"workspace")
    execution = execution_provenance(workspace)
    canonical = publish_synthetic(fit_directory, execution)
    sidecar = Path(provenance.final_provenance_path(canonical))
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    record["runtime_semantic_argv"][-1] = "different_algorithm"
    record["identity_semantic_argv"][-1] = "different_algorithm"
    sidecar.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(RuntimeError, match="job_set_id is inconsistent"):
        provenance.validate_final_provenance(sidecar)


def test_final_provenance_validates_after_package_relocation_without_workspace(
    tmp_path,
):
    original_directory = tmp_path / "original" / "fit_files"
    relocated_directory = tmp_path / "relocated" / "fit_files"
    original_directory.mkdir(parents=True)
    relocated_directory.mkdir(parents=True)
    workspace = tmp_path / "original" / "workspace.root"
    workspace.write_bytes(b"workspace")
    execution = execution_provenance(workspace)
    canonical = publish_synthetic(original_directory, execution)
    sidecar = Path(provenance.final_provenance_path(canonical))
    relocated_canonical = relocated_directory / canonical.name
    relocated_sidecar = Path(provenance.final_provenance_path(relocated_canonical))
    shutil.copy2(canonical, relocated_canonical)
    shutil.copy2(sidecar, relocated_sidecar)
    workspace.unlink()
    assert provenance.validate_final_provenance(relocated_sidecar) == str(
        relocated_canonical.resolve()
    )


def test_transient_alias_is_relative_locked_and_context_bounded(tmp_path):
    fit_directory = tmp_path / "fit_files"
    fit_directory.mkdir()
    workspace = tmp_path / "workspace.root"
    workspace.write_bytes(b"workspace")
    execution = execution_provenance(workspace)
    canonical = publish_synthetic(fit_directory, execution)
    with provenance.transient_legacy_alias(
        execution["scan_name"], execution["job_set_id"], fit_directory
    ) as alias_value:
        alias = Path(alias_value)
        assert alias.is_symlink()
        assert not os.path.isabs(os.readlink(alias))
        assert alias.resolve() == canonical.resolve()
        competing_descriptor = os.open(fit_directory, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(
                    competing_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
                )
        finally:
            os.close(competing_descriptor)
    assert not alias.exists()
    assert not alias.is_symlink()
    assert canonical.read_bytes() == b"result"


@pytest.mark.parametrize("collision", ["regular", "other_symlink"])
def test_transient_alias_preserves_and_rejects_collisions(tmp_path, collision):
    fit_directory = tmp_path / "fit_files"
    fit_directory.mkdir()
    workspace = tmp_path / "workspace.root"
    workspace.write_bytes(b"workspace")
    execution = execution_provenance(workspace)
    canonical = publish_synthetic(fit_directory, execution)
    alias = fit_directory / "higgsCombine{}.MultiDimFit.root".format(
        execution["scan_name"]
    )
    if collision == "regular":
        alias.write_bytes(b"legacy")
    else:
        other = fit_directory / "other.root"
        other.write_bytes(b"other")
        alias.symlink_to(other.name)
    with pytest.raises(RuntimeError, match="Compatibility alias"):
        with provenance.transient_legacy_alias(
            execution["scan_name"], execution["job_set_id"], fit_directory
        ):
            pass
    assert os.path.lexists(alias)
    assert canonical.read_bytes() == b"result"


def test_provenance_bound_legacy_adoption_preserves_bytes(tmp_path):
    fit_directory = tmp_path / "fit_files"
    fit_directory.mkdir()
    workspace = tmp_path / "workspace.root"
    workspace.write_bytes(b"workspace")
    execution = execution_provenance(workspace)
    execution_path = tmp_path / "execution_provenance.json"
    execution_path.write_text(json.dumps(execution), encoding="utf-8")
    legacy = fit_directory / "higgsCombine{}.MultiDimFit.root".format(
        execution["scan_name"]
    )
    legacy.write_bytes(b"legacy result bytes")
    old_hash = provenance.sha256_file(legacy)
    canonical = Path(
        provenance.adopt_legacy_aggregate(
            execution["scan_name"],
            execution["job_set_id"],
            execution_path,
            fit_directory,
        )
    )
    assert not legacy.exists()
    assert canonical.read_bytes() == b"legacy result bytes"
    assert provenance.sha256_file(canonical) == old_hash
    assert provenance.validate_final_provenance(
        provenance.final_provenance_path(canonical),
        execution["scan_name"],
        execution["job_set_id"],
    ) == str(canonical.resolve())


def test_legacy_adoption_rejects_unknown_identity_and_conflict(tmp_path):
    fit_directory = tmp_path / "fit_files"
    fit_directory.mkdir()
    workspace = tmp_path / "workspace.root"
    workspace.write_bytes(b"workspace")
    execution = execution_provenance(workspace)
    execution_path = tmp_path / "execution_provenance.json"
    invalid = dict(execution)
    invalid["job_set_id"] = "0" * 64
    execution_path.write_text(json.dumps(invalid), encoding="utf-8")
    legacy = fit_directory / "higgsCombine{}.MultiDimFit.root".format(
        execution["scan_name"]
    )
    legacy.write_bytes(b"legacy")
    with pytest.raises(RuntimeError, match="inconsistent"):
        provenance.adopt_legacy_aggregate(
            execution["scan_name"],
            execution["job_set_id"],
            execution_path,
            fit_directory,
        )
    assert legacy.read_bytes() == b"legacy"

    execution_path.write_text(json.dumps(execution), encoding="utf-8")
    canonical = fit_directory / provenance.canonical_aggregate_name(
        execution["scan_name"], execution["job_set_id"]
    )
    canonical.write_bytes(b"conflict")
    with pytest.raises(RuntimeError, match="already exists"):
        provenance.adopt_legacy_aggregate(
            execution["scan_name"],
            execution["job_set_id"],
            execution_path,
            fit_directory,
        )
    assert legacy.read_bytes() == b"legacy"
    assert canonical.read_bytes() == b"conflict"


def write_fake_workspace_executable(path, mode):
    body = """#!/bin/sh
output=
printf '%s\\n' "$@" > "$FAKE_ARGUMENT_LOG"
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then
    output=$2
    shift 2
  else
    shift
  fi
done
{operation}
"""
    if mode == "success":
        operation = "printf replacement > \"$output\""
    elif mode == "empty":
        operation = ": > \"$output\""
    else:
        operation = "false"
    path.write_text(body.format(operation=operation), encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize(
    "initial,overwrite,mode,expected,success",
    [
        (None, False, "success", b"replacement", True),
        (b"old", False, "success", b"old", False),
        (b"old", True, "success", b"replacement", True),
        (b"old", True, "failure", b"old", False),
        (b"old", True, "empty", b"old", False),
    ],
)
def test_shell_workspace_wrapper_overwrite_and_model_arguments(
    tmp_path, initial, overwrite, mode, expected, success
):
    target = tmp_path / "workspace.root"
    if initial is not None:
        target.write_bytes(initial)
    fake = tmp_path / "fake-text2workspace"
    argument_log = tmp_path / "arguments.txt"
    write_fake_workspace_executable(fake, mode)
    command = [
        "bash",
        str(FITTER_DIR / "scripts" / "text2workspace.sh"),
        "--output",
        str(target),
        "--executable",
        str(fake),
    ]
    if overwrite:
        command.append("--overwrite")
    environment = os.environ.copy()
    environment["FAKE_ARGUMENT_LOG"] = str(argument_log)
    result = subprocess.run(command, cwd=tmp_path, env=environment, check=False)
    assert (result.returncode == 0) == success
    assert target.read_bytes() == expected
    assert list(tmp_path.glob(".workspace.root.tmp.*")) == []
    if argument_log.exists():
        arguments = argument_log.read_text(encoding="utf-8").splitlines()
        output_index = arguments.index("-o")
        assert arguments[:output_index] == [
            "combinedcard.txt",
            "-P",
            "HiggsAnalysis.CombinedLimit.InterferenceModels:interferenceModel",
            "--PO",
            "scalingData=scalings.json",
            "--PO",
            "verbose",
        ]
        assert len(arguments) == output_index + 2

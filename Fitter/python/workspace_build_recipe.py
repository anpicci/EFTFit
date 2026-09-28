"""Explicit workspace recipes and the shared text2workspace command owner.

The YAML loader resolves locators relative to the recipe file. Legacy adapters
may construct the same value object with their historical caller-relative paths.
No Combine command is run by loading or building an argv.
"""

import argparse
from dataclasses import dataclass
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

if __package__:
    from .execution_provenance import file_identity, workspace_identity
else:
    from execution_provenance import file_identity, workspace_identity


@dataclass(frozen=True)
class WorkspaceBuildConfig:
    combined_card: str
    plugin: str
    workspace: str
    parameterization_option: str = ""
    parameterization_source: str = ""
    model_options: tuple = ()
    channel_masks: bool = False
    allow_no_background: bool = False
    executable: str = "text2workspace.py"
    software: object = None
    existing_policy: str = "fail"


def _mapping(value, label, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError("{} must contain {} and only permitted fields".format(label, ", ".join(required)))
    return value


def _nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{} must be a nonempty string".format(label))
    return value


def _locator(value, base_dir):
    path = Path(_nonempty(value, "locator"))
    return str(path if path.is_absolute() else base_dir / path)


def normalize_workspace_build_recipe(data, base_dir):
    """Parse supported YAML schema v1; version is parser compatibility only."""
    _mapping(data, "workspace recipe", ("schema_version", "input", "model", "output"),
             ("parameterization", "build", "software"))
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported workspace recipe schema_version")
    card = _mapping(data["input"], "input", ("combined_card",))
    model = _mapping(data["model"], "model", ("plugin",), ("options",))
    output = _mapping(data["output"], "output", ("workspace",), ("existing_policy",))
    existing_policy = output.get("existing_policy", "fail")
    if existing_policy not in ("fail", "overwrite", "new_version"):
        raise ValueError("output.existing_policy must be fail, overwrite or new_version")
    build = _mapping(data.get("build", {}), "build", (), ("channel_masks", "allow_no_background"))
    parameterization = data.get("parameterization")
    if parameterization is not None:
        _mapping(parameterization, "parameterization", ("option", "source"))
        option = _nonempty(parameterization["option"], "parameterization.option")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", option) or option == "wcs":
            raise ValueError("invalid parameterization option")
        source = _locator(parameterization["source"], Path(base_dir))
    else:
        option, source = "", ""
    options = model.get("options", [])
    if not isinstance(options, list) or not all(isinstance(item, str) and item.strip() for item in options):
        raise ValueError("model.options must be a list of nonempty strings")
    reserved = ("fits=", "scalingData=", "scaling=", "wcs=", option + "=") if option else ("fits=", "scalingData=", "scaling=", "wcs=")
    if any(item.startswith(reserved) for item in options):
        raise ValueError("parameterization and WC population cannot be duplicated in model.options")
    for flag, value in build.items():
        if type(value) is not bool:
            raise ValueError("build.{} must be boolean".format(flag))
    software = data.get("software", {})
    if not isinstance(software, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in software.items()):
        raise ValueError("software must be a string mapping")
    return WorkspaceBuildConfig(
        combined_card=_locator(card["combined_card"], Path(base_dir)),
        plugin=_nonempty(model["plugin"], "model.plugin"),
        workspace=_locator(output["workspace"], Path(base_dir)),
        parameterization_option=option,
        parameterization_source=source,
        model_options=tuple(options),
        channel_masks=build.get("channel_masks", False),
        allow_no_background=build.get("allow_no_background", False),
        software=dict(software),
        existing_policy=existing_policy,
    )


def load_workspace_build_recipe(path):
    import yaml

    path = Path(path).resolve()
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    return normalize_workspace_build_recipe(data, path.parent)


def build_workspace_argv(config, output=None):
    """Build only argv; locators and provenance never trigger live Git checks."""
    argv = [config.executable, config.combined_card, "-P", config.plugin]
    if config.parameterization_option:
        argv.extend(("--PO", "{}={}".format(config.parameterization_option, config.parameterization_source)))
    for option in config.model_options:
        argv.extend(("--PO", option))
    argv.extend(("-o", str(output if output is not None else config.workspace)))
    if config.channel_masks:
        argv.append("--channel-masks")
    if config.allow_no_background:
        argv.append("--X-allow-no-background")
    return argv


def _observed_software(config):
    """Observe implementations for a qualified build, never a consumer gate."""
    eftfit = Path(__file__).resolve().parents[2]
    cmssw_base = os.environ.get("CMSSW_BASE")
    if not cmssw_base:
        raise RuntimeError("CMSSW_BASE is required for qualified build provenance")
    combined_limit = Path(cmssw_base) / "src/HiggsAnalysis/CombinedLimit"
    expected_executable = combined_limit / "scripts/text2workspace.py"
    observed_executable = shutil.which(config.executable)
    if not observed_executable or os.path.realpath(observed_executable) != os.path.realpath(expected_executable):
        raise RuntimeError("text2workspace executable does not resolve to the observed CombinedLimit tree")

    def require_clean(path, sources):
        dirty = subprocess.run(
            ["git", "-C", str(path), "diff", "--quiet", "HEAD", "--", *sources],
            check=False,
        )
        if dirty.returncode != 0:
            raise RuntimeError("Task-relevant workspace build source is not clean at its commit")

    eftfit_sources = ["Fitter/python/workspace_build_recipe.py"]
    if config.plugin.startswith("EFTFit.Fitter.EFTModel:"):
        eftfit_sources.append("Fitter/python/EFTModel.py")
    require_clean(eftfit, eftfit_sources)
    combined_sources = ["scripts/text2workspace.py"]
    if config.plugin.startswith("HiggsAnalysis.CombinedLimit.InterferenceModels:"):
        combined_sources.append("python/InterferenceModels.py")
    if config.plugin.startswith("HiggsAnalysis.CombinedLimit.PhysicsModel:"):
        combined_sources.append("python/PhysicsModel.py")
    require_clean(combined_limit, combined_sources)

    def commit(path):
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        )
        value = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise RuntimeError("Invalid observed software commit: {}".format(path))
        return value

    observed = {
        "eftfit_commit": commit(eftfit),
        "combined_limit_commit": commit(combined_limit),
        "cmssw_release": os.environ.get("CMSSW_VERSION"),
        "scram_arch": os.environ.get("SCRAM_ARCH"),
        "text2workspace_executable": os.path.realpath(observed_executable),
    }
    if "CombineHarvester" in config.plugin:
        observed["combine_harvester_commit"] = commit(Path(cmssw_base) / "src/CombineHarvester")
    return observed


def _versioned_sibling(target):
    path = Path(target)
    for number in range(1, 1000):
        candidate = path.with_name("{}.v{:03d}{}".format(path.stem, number, path.suffix))
        if not os.path.lexists(candidate):
            return str(candidate)
    raise RuntimeError("No available workspace version sibling")


def run_workspace_build(config, overwrite=False, process_factory=None, output_handler=None,
                        new_version=False):
    """Build at a temporary locator, then publish according to explicit policy."""
    if overwrite and new_version:
        raise ValueError("overwrite and new_version are mutually exclusive")
    if config.existing_policy not in ("fail", "overwrite", "new_version"):
        raise ValueError("invalid existing_policy")
    if (overwrite or new_version) and config.existing_policy != "fail":
        raise ValueError("CLI policy conflicts with recipe existing_policy")
    policy = "overwrite" if overwrite else "new_version" if new_version else config.existing_policy
    requested = os.path.abspath(config.workspace)
    target = requested
    target_directory = os.path.dirname(target)
    if not os.path.isdir(target_directory):
        raise RuntimeError("Workspace target directory does not exist: {}".format(target_directory))
    predecessor = None
    if os.path.lexists(requested):
        if os.path.islink(target) or not os.path.isfile(target):
            raise RuntimeError("Workspace target is not a regular file: {}".format(target))
        if policy == "fail":
            raise RuntimeError("Workspace target already exists: {}".format(target))
        predecessor = {"path": requested, **file_identity(requested)}
        if policy == "new_version":
            target = _versioned_sibling(requested)
    known_plugin = config.plugin.startswith((
        "EFTFit.Fitter.EFTModel:",
        "HiggsAnalysis.CombinedLimit.InterferenceModels:",
        "HiggsAnalysis.CombinedLimit.PhysicsModel:",
    ))
    qualified = (known_plugin and config.executable == "text2workspace.py" and
                 process_factory in (None, subprocess.Popen))
    observed_software = _observed_software(config) if qualified else None
    input_identity = {
        "combined_card_sha256": file_identity(config.combined_card)["sha256"] if os.path.isfile(config.combined_card) else None,
        "parameterization_sha256": file_identity(config.parameterization_source)["sha256"] if config.parameterization_source and os.path.isfile(config.parameterization_source) else None,
    }
    if qualified and (not input_identity["combined_card_sha256"] or
                      config.parameterization_option and not input_identity["parameterization_sha256"]):
        raise RuntimeError("Qualified workspace build inputs must be readable files")
    descriptor, temporary = tempfile.mkstemp(
        prefix=".{}.".format(os.path.basename(target)), suffix=".tmp", dir=target_directory
    )
    os.close(descriptor)
    os.unlink(temporary)
    try:
        argv = build_workspace_argv(config, temporary)
        if qualified:
            argv[0] = observed_software["text2workspace_executable"]
        factory = process_factory or subprocess.Popen
        if output_handler is None:
            process = factory(argv)
            return_code = process.wait()
        else:
            process = factory(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate()
            output_handler(io.BytesIO(stdout or b""), "info")
            output_handler(io.BytesIO(stderr or b""), "err")
            return_code = process.returncode
        if return_code:
            raise RuntimeError("text2workspace command failed with code {}".format(return_code))
        if not os.path.isfile(temporary) or os.stat(temporary).st_size == 0:
            raise RuntimeError("text2workspace did not produce a nonempty workspace replacement")
        artifact = file_identity(temporary)
        if qualified and _observed_software(config) != observed_software:
            raise RuntimeError("Workspace build source provenance changed during execution")
        if policy == "overwrite" and predecessor is not None:
            if file_identity(requested) != {key: predecessor[key] for key in ("resolved_path", "size_bytes", "sha256")}:
                raise RuntimeError("Workspace predecessor changed during build")
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
            os.unlink(temporary)
        identity = workspace_identity(target)
        return {
            "schema_version": 1,
            "requested_output_path": requested,
            "resolved_output_path": target,
            "existing_policy": policy,
            "workspace": {"sha256": identity["sha256"], "size_bytes": artifact["size_bytes"]},
            "sha256": identity["sha256"],
            "predecessor": predecessor,
            "replaced_artifact": predecessor if policy == "overwrite" else None,
            "input": input_identity,
            "recipe": {"schema_version": 1, "declared_software": config.software},
            "observed_software": observed_software,
            "qualified_provenance": qualified,
        }
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build a workspace from an explicit YAML recipe")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--recipe")
    source.add_argument("--legacy-interference", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--executable", default="text2workspace.py")
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument("--overwrite", action="store_true")
    policy.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    if args.legacy_interference:
        config = WorkspaceBuildConfig(
            "combinedcard.txt",
            "HiggsAnalysis.CombinedLimit.InterferenceModels:interferenceModel",
            args.output or "workspace.root",
            "scalingData", "scalings.json", ("verbose",), executable=args.executable,
        )
    else:
        config = load_workspace_build_recipe(args.recipe)
        if args.output or args.executable != "text2workspace.py":
            from dataclasses import replace
            config = replace(config, workspace=args.output or config.workspace, executable=args.executable)
    run_workspace_build(config, overwrite=args.overwrite, new_version=args.new_version)


if __name__ == "__main__":
    main()

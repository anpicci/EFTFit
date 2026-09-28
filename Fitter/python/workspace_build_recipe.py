"""Explicit workspace recipes and the shared text2workspace command owner.

The YAML loader resolves locators relative to the recipe file. Legacy adapters
may construct the same value object with their historical caller-relative paths.
No Combine command is run by loading or building an argv.
"""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import tempfile

if __package__:
    from .execution_provenance import workspace_identity
else:
    from execution_provenance import workspace_identity


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
    output = _mapping(data["output"], "output", ("workspace",))
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


def run_workspace_build(config, overwrite=False, process_factory=None, output_handler=None):
    """Guarded publication seam; caller must explicitly opt in to replacement."""
    target = os.path.abspath(config.workspace)
    target_directory = os.path.dirname(target)
    if not os.path.isdir(target_directory):
        raise RuntimeError("Workspace target directory does not exist: {}".format(target_directory))
    if os.path.lexists(target):
        if os.path.islink(target) or not os.path.isfile(target):
            raise RuntimeError("Workspace target is not a regular file: {}".format(target))
        if not overwrite:
            raise RuntimeError("Workspace target already exists; pass overwrite to replace it: {}".format(target))
    descriptor, temporary = tempfile.mkstemp(
        prefix=".{}.".format(os.path.basename(target)), suffix=".tmp", dir=target_directory
    )
    os.close(descriptor)
    os.unlink(temporary)
    try:
        argv = build_workspace_argv(config, temporary)
        factory = process_factory or subprocess.Popen
        process = factory(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with process.stdout, process.stderr:
            if output_handler is not None:
                output_handler(process.stdout, "info")
                output_handler(process.stderr, "err")
            else:
                process.stdout.read()
                process.stderr.read()
        return_code = process.wait()
        if return_code:
            raise RuntimeError("text2workspace command failed with code {}".format(return_code))
        if not os.path.isfile(temporary) or os.stat(temporary).st_size == 0:
            raise RuntimeError("text2workspace did not produce a nonempty workspace replacement")
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
            os.unlink(temporary)
        return workspace_identity(config.workspace)
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
    parser.add_argument("--overwrite", action="store_true")
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
    run_workspace_build(config, overwrite=args.overwrite)


if __name__ == "__main__":
    main()

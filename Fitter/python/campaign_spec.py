"""Single user YAML and frozen, package-bound 1D campaign specification."""

import hashlib
import json
import math
from pathlib import Path
import re

import yaml

from .execution_provenance import (canonical_json_bytes, file_identity, workspace_identity,
                                   normalize_grid_indices)
from .inference_recipe import normalize_inference_recipe, bind_inference_invocation
from .wc_fit_config import load_fit_input_package, load_fit_config, resolve_scan_wcs, WCFitConfig


def digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class campaign_yaml_loader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError("YAML keys must be unique strings")
        result[key] = loader.construct_object(value_node)
    return result


campaign_yaml_loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or set(value) - set(required) - set(optional):
        raise ValueError("expected fields {}, optional {}".format(sorted(required), sorted(optional)))


def load_spec(path):
    path = Path(path).resolve()
    source = path.read_text(encoding="utf-8")
    data = yaml.load(source, Loader=campaign_yaml_loader)
    fields(data, {"schema_version", "package", "workspace", "dataset", "selection", "grid"},
           {"fit_start_values", "backend", "keep_failures", "local_runtime", "condor_runtime"})
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported campaign schema_version")
    if data.get("keep_failures", False) is not False:
        raise ValueError("--keepFailures is unqualified: initial-fit/special-entry failure semantics remain open")
    fields(data["package"], {"path", "sha256"})
    fields(data["workspace"], {"path", "sha256"})
    def input_path(value):
        if not isinstance(value, str) or not value:
            raise ValueError("input path must be a nonempty string")
        return (path.parent / value).resolve()
    package = load_fit_input_package(input_path(data["package"]["path"]))
    package_identity = file_identity(package.config_path)
    config = load_fit_config(package.config_path)
    if file_identity(package.config_path) != package_identity:
        raise ValueError("package configuration changed during validation")
    workspace = workspace_identity(input_path(data["workspace"]["path"]))
    for label, observed in (("package", package_identity), ("workspace", workspace)):
        if data[label]["sha256"] != observed["sha256"]:
            raise ValueError(label + " sha256 mismatch")
    dataset = data["dataset"]
    if not isinstance(dataset, dict) or dataset.get("mode") not in ("observed", "expected"):
        raise ValueError("dataset.mode must be observed or expected")
    if dataset["mode"] == "observed":
        fields(dataset, {"mode"}, {"name", "producer_label"})
        name = dataset.get("name", "data_obs")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("dataset.name must name a workspace dataset")
        effective_dataset = dict(dataset, name=name)
    else:
        fields(dataset, {"mode", "truth"}, {"nuisance_generation", "producer_label"})
        if dataset["truth"] != "sm":
            raise ValueError("expected generation truth currently supports only sm")
        if dataset.get("nuisance_generation", "nominal") != "nominal":
            raise ValueError("frequentist nuisance generation requires verified source data and is unqualified")
        effective_dataset = dict(dataset, nuisance_generation="nominal")
    if "producer_label" in dataset and dataset["producer_label"] not in ("blinded", "unblinded"):
        raise ValueError("dataset.producer_label must be blinded or unblinded")
    fields(data["selection"], {"wcs", "modes"})
    wcs = data["selection"]["wcs"]
    if wcs == "all_scientific":
        wcs = list(config.scientific_wcs)
    if not isinstance(wcs, list) or not wcs or not all(isinstance(wc, str) for wc in wcs):
        raise ValueError("selection.wcs must be all_scientific or a nonempty WC list")
    wcs = list(resolve_scan_wcs(config, wcs))
    modes = data["selection"]["modes"]
    if not isinstance(modes, list) or not modes or any(mode not in ("profiled", "frozen") for mode in modes) or len(modes) != len(set(modes)):
        raise ValueError("selection.modes must be a nonempty unique list of profiled/frozen")
    fields(data["grid"], {"points", "align_edges"}, {"selected_indices"})
    count = data["grid"]["points"]
    if type(count) is not int or count < 2 or type(data["grid"]["align_edges"]) is not bool:
        raise ValueError("grid requires explicit integer points >= 2 and boolean align_edges")
    effective_grid = dict(data["grid"])
    if "selected_indices" in effective_grid:
        effective_grid["selected_indices"], _ = normalize_grid_indices(
            count, effective_grid["selected_indices"])
    starts = data.get("fit_start_values", {})
    for mode in modes:
        recipe = normalize_inference_recipe({"schema_version": 1, "family": "scan_1d_" + mode,
                                             "dataset_mode": "observed" if dataset["mode"] == "observed" else "asimov",
                                             "fit_start_values": starts}, config)
        for wc in wcs:
            bind_inference_invocation(recipe, config, axes=(wc,))
    if any(value != 0 for value in starts.values()):
        raise ValueError("nonzero fit_start_values are unsupported by the configured grid argv builder")
    backend = data.get("backend")
    if backend is not None and (not isinstance(backend, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", backend)):
        raise ValueError("backend must be a provider name")
    runtime_bindings = {}
    for runtime_name in ("local_runtime", "condor_runtime"):
        binding = data.get(runtime_name)
        if binding is None:
            continue
        fields(binding, {"cmsset_default", "scram_arch", "release_src"})
        if not isinstance(binding["scram_arch"], str) or not re.fullmatch(
                r"[A-Za-z0-9_]+", binding["scram_arch"]):
            raise ValueError(runtime_name + ".scram_arch must be a simple architecture name")
        binding = dict(binding)
        for key in ("cmsset_default", "release_src"):
            if not isinstance(binding[key], str) or not binding[key]:
                raise ValueError(runtime_name + " paths must be nonempty strings")
            binding[key] = str(input_path(binding[key]))
        runtime_bindings[runtime_name] = binding
    effective = {"schema_version": 1, "package": package_identity, "workspace": workspace,
                 "dataset": effective_dataset, "selection": {"wcs": wcs, "modes": modes},
                 "grid": effective_grid, "fit_start_values": starts, "default_backend": backend,
                 "keep_failures": False,
                 "fit_config": {"analysis": config.analysis, "scientific_wcs": config.scientific_wcs,
                                "fixed_wcs": config.fixed_wcs, "wc_ranges": config.wc_ranges}}
    effective.update(runtime_bindings)
    # JSON normalization makes in-memory and reopened representations identical.
    return json.loads(canonical_json_bytes(effective)), source


def fit_config(spec):
    config = spec["fit_config"]
    return WCFitConfig(config["analysis"], tuple(config["scientific_wcs"]), tuple(config["fixed_wcs"]), config["wc_ranges"])


def planned_points(spec):
    count = spec["grid"]["points"]
    result = []
    for wc in spec["selection"]["wcs"]:
        lower, upper = spec["fit_config"]["wc_ranges"][wc]
        for mode in spec["selection"]["modes"]:
            previous = None
            for index in range(count):
                fraction = index / (count - 1) if spec["grid"]["align_edges"] else (index + 0.5) / count
                coordinate = upper if spec["grid"]["align_edges"] and index == count - 1 else lower + (upper - lower) * fraction
                if not math.isfinite(coordinate) or (previous is not None and coordinate <= previous):
                    raise ValueError("grid coordinates must be finite and distinguishable at machine precision")
                result.append({"scan_id": wc + ":" + mode, "wc": wc, "mode": mode,
                               "point_index": index, "coordinate": coordinate})
                previous = coordinate
    return result

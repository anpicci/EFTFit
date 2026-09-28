"""YAML inference recipe v1; no execution or result lifecycle is owned here."""

from dataclasses import dataclass
import math
from pathlib import Path

import yaml


_SCAN_FAMILIES = {
    "scan_1d_profiled": (1, "profiled"),
    "scan_1d_frozen": (1, "frozen"),
    "scan_2d_profiled": (2, "profiled"),
    "scan_2d_frozen": (2, "frozen"),
}
_IMPACT_FAMILIES = {"impacts_frozen", "impacts_profiled"}
_NUISANCE_SELECTOR = "modelconfig_free_non_wc"


@dataclass(frozen=True)
class InferenceRecipe:
    family: str
    dataset_mode: str
    reference_values: dict
    fit_start_values: dict
    nuisance_selector: str = ""


def _numeric_mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError("{} must be a mapping".format(label))
    for key, number in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError("{} keys must be nonempty strings".format(label))
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number):
            raise ValueError("{} values must be finite numbers".format(label))
    return dict(value)


def _validate_start_values(starts, fit_config):
    scientific = set(fit_config.scientific_wcs)
    if set(starts) - scientific:
        raise ValueError("fit_start_values must contain scientific WCs only")
    for wc, value in starts.items():
        lower, upper = fit_config.wc_ranges[wc]
        if not lower <= value <= upper:
            raise ValueError("fit_start_values.{} is outside configured range".format(wc))


def normalize_inference_recipe(data, fit_config=None):
    """Validate family state; fit config remains the only WC/range authority."""
    if not isinstance(data, dict) or not {"schema_version", "family", "dataset_mode"} <= set(data):
        raise ValueError("inference recipe requires schema_version, family and dataset_mode")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported inference recipe schema_version")
    family = data["family"]
    if not isinstance(family, str) or (family not in _SCAN_FAMILIES and family not in _IMPACT_FAMILIES):
        raise ValueError("unsupported inference recipe family")
    if data["dataset_mode"] not in ("observed", "asimov"):
        raise ValueError("unsupported dataset_mode")
    allowed = {"schema_version", "family", "dataset_mode", "fit_start_values"}
    if family in _IMPACT_FAMILIES:
        allowed.add("impacts")
    if set(data) - allowed:
        raise ValueError("fields do not belong to this inference recipe family")
    starts = _numeric_mapping(data.get("fit_start_values", {}), "fit_start_values")
    impacts = data.get("impacts", {})
    if family in _IMPACT_FAMILIES:
        if not isinstance(impacts, dict) or set(impacts) - {"reference_values", "nuisance_selector"}:
            raise ValueError("impacts must contain only reference_values and nuisance_selector")
        selector = impacts.get("nuisance_selector", _NUISANCE_SELECTOR)
        if selector != _NUISANCE_SELECTOR:
            raise ValueError("unsupported nuisance selector")
        references = _numeric_mapping(impacts.get("reference_values", {}), "reference_values")
    else:
        selector, references = "", {}
    if fit_config is not None:
        scientific = set(fit_config.scientific_wcs)
        if set(references) - scientific:
            raise ValueError("reference_values must contain scientific WCs only")
        _validate_start_values(starts, fit_config)
        if family in _IMPACT_FAMILIES:
            references = {wc: references.get(wc, 0) for wc in fit_config.scientific_wcs}
            for wc, value in references.items():
                lower, upper = fit_config.wc_ranges[wc]
                if not lower <= value <= upper:
                    raise ValueError("reference_values.{} is outside configured range".format(wc))
    return InferenceRecipe(family, data["dataset_mode"], references, starts, selector)


def load_inference_recipe(path, fit_config=None):
    with Path(path).open(encoding="utf-8") as stream:
        return normalize_inference_recipe(yaml.safe_load(stream), fit_config)


def _bind_reference_values(recipe, fit_config):
    scientific = set(fit_config.scientific_wcs)
    if set(recipe.reference_values) - scientific:
        raise ValueError("reference_values must contain scientific WCs only")
    resolved = {wc: recipe.reference_values.get(wc, 0) for wc in fit_config.scientific_wcs}
    for wc, value in resolved.items():
        lower, upper = fit_config.wc_ranges[wc]
        if not lower <= value <= upper:
            raise ValueError("reference_values.{} is outside configured range".format(wc))
    return resolved


def bind_inference_invocation(recipe, fit_config, axes=(), report_targets=()):
    """Bind concrete axes or report targets without persisting an execution identity."""
    axes = tuple(axes)
    targets = tuple(report_targets)
    scientific = set(fit_config.scientific_wcs)
    _validate_start_values(recipe.fit_start_values, fit_config)
    if recipe.family in _SCAN_FAMILIES:
        dimension, other_policy = _SCAN_FAMILIES[recipe.family]
        if len(axes) != dimension or len(set(axes)) != dimension or set(axes) - scientific:
            raise ValueError("scan axes must be distinct configured scientific WCs")
        if targets:
            raise ValueError("scan invocation does not use report_targets")
        # Grid axes may initialize the initial fit, but never set grid points.
        # Other scientific WCs are unavailable as starts in a frozen scan.
        active = scientific if other_policy == "profiled" else set(axes)
        if set(recipe.fit_start_values) - active:
            raise ValueError("fit_start_values contains WCs fixed by this scan")
        return {
            "family": recipe.family,
            "dataset_mode": recipe.dataset_mode,
            "axes": axes,
            "other_scientific_policy": other_policy,
            "other_scientific_wcs": tuple(wc for wc in fit_config.scientific_wcs if wc not in axes),
            "fixed_zero_wcs": fit_config.fixed_wcs,
            "wc_ranges": fit_config.wc_ranges,
            "fit_start_values": recipe.fit_start_values,
        }
    if axes:
        raise ValueError("impacts invocation does not use scan axes")
    if not targets or len(set(targets)) != len(targets) or set(targets) - scientific:
        raise ValueError("report_targets must be configured scientific WCs")
    if recipe.family == "impacts_frozen" and len(targets) != 1:
        raise ValueError("impacts_frozen requires one target per production")
    active = scientific if recipe.family == "impacts_profiled" else set(targets)
    if set(recipe.fit_start_values) - active:
        raise ValueError("fit_start_values contains WCs fixed by this impact")
    reference_values = _bind_reference_values(recipe, fit_config)
    return {
        "family": recipe.family,
        "dataset_mode": recipe.dataset_mode,
        "report_targets": targets,
        "scientific_policy": "all_profiled" if recipe.family == "impacts_profiled" else "other_fixed_at_reference",
        "scientific_wcs": fit_config.scientific_wcs,
        "fixed_zero_wcs": fit_config.fixed_wcs,
        "wc_ranges": fit_config.wc_ranges,
        "reference_values": reference_values,
        "fit_start_values": recipe.fit_start_values,
        "nuisance_selector": recipe.nuisance_selector,
    }

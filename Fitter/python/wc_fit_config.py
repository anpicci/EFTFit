"""Load and materialize analysis-owned WC population configuration."""

from dataclasses import dataclass
from pathlib import Path
import re

import yaml


_CONTROL_OPTIONS = {
    "--redefineSignalPOIs",
    "--floatOtherPOIs",
    "--freezeParameters",
    "--setParameters",
}


@dataclass(frozen=True)
class WCFitConfig:
    analysis: str
    scientific_wcs: tuple
    fixed_wcs: tuple


def validate_wc_population(scientific_wcs, fixed_wcs):
    """Validate and return the two ordered, disjoint WC populations."""
    scientific = tuple(scientific_wcs)
    fixed = tuple(fixed_wcs)

    for label, values in (("scientific", scientific), ("fixed_zero", fixed)):
        if not values:
            raise ValueError("wc_population.{} must not be empty".format(label))
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("wc_population.{} must contain non-empty strings".format(label))
        if len(values) != len(set(values)):
            raise ValueError("wc_population.{} contains duplicate WCs".format(label))

    overlap = set(scientific) & set(fixed)
    if overlap:
        raise ValueError(
            "scientific and fixed_zero WC populations overlap: {}".format(
                ",".join(sorted(overlap))
            )
        )
    return scientific, fixed


def load_fit_config(path):
    """Load the minimal per-analysis YAML fit configuration at ``path``."""
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)

    if not isinstance(data, dict) or set(data) != {"analysis", "wc_population"}:
        raise ValueError("fit config must contain only analysis and wc_population")
    if not isinstance(data["analysis"], str) or not data["analysis"]:
        raise ValueError("analysis must be a non-empty string")
    population = data["wc_population"]
    if not isinstance(population, dict) or set(population) != {"scientific", "fixed_zero"}:
        raise ValueError("wc_population must contain only scientific and fixed_zero")
    if not isinstance(population["scientific"], list) or not isinstance(population["fixed_zero"], list):
        raise ValueError("scientific and fixed_zero must be lists")

    scientific, fixed = validate_wc_population(
        population["scientific"], population["fixed_zero"]
    )
    return WCFitConfig(data["analysis"], scientific, fixed)


def resolve_scan_wcs(config, scan_wcs=None):
    """Return invocation-local scan WCs, defaulting to all scientific WCs."""
    resolved = tuple(config.scientific_wcs if not scan_wcs else scan_wcs)
    if len(resolved) != len(set(resolved)):
        raise ValueError("scan_wcs contains duplicate WCs")
    invalid = set(resolved) - set(config.scientific_wcs)
    if invalid:
        raise ValueError(
            "scan_wcs must be a subset of scientific WCs; invalid: {}".format(
                ",".join(sorted(invalid))
            )
        )
    return resolved


def _split_option_tokens(arguments):
    tokens = []
    for argument in list(arguments or ()):
        if not isinstance(argument, str):
            raise ValueError("Combine arguments must be strings")
        stripped = argument.strip()
        matching_equals_option = next(
            (
                option
                for option in _CONTROL_OPTIONS
                if stripped.startswith(option + "=")
            ),
            None,
        )
        if matching_equals_option is not None:
            tokens.extend(
                [matching_equals_option, stripped[len(matching_equals_option) + 1 :]]
            )
        elif stripped.startswith("--") and any(char.isspace() for char in stripped):
            option, value = stripped.split(None, 1)
            tokens.extend([option, value])
        else:
            tokens.append(argument)
    return tokens


def _parse_csv(value, option):
    entries = [entry.strip() for entry in value.split(",") if entry.strip()]
    if not entries:
        raise ValueError("{} requires a non-empty value".format(option))
    return entries


def _is_zero(value):
    try:
        return float(value) == 0.0
    except ValueError:
        return False


def _freeze_selector_matches_scientific(parameter, scientific_wcs):
    if parameter in scientific_wcs:
        return True
    if parameter.startswith("rgx{") and parameter.endswith("}"):
        expression = parameter[4:-1]
        try:
            matcher = re.compile(expression)
        except re.error as error:
            raise ValueError("invalid --freezeParameters regex: {}".format(error))
        return any(matcher.fullmatch(wc) for wc in scientific_wcs)
    return False


def _collect_control_values(arguments):
    passthrough = []
    controls = {option: [] for option in _CONTROL_OPTIONS}
    tokens = _split_option_tokens(arguments)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token not in _CONTROL_OPTIONS:
            passthrough.append(token)
            index += 1
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
            raise ValueError("{} requires a value".format(token))
        controls[token].append(tokens[index + 1])
        index += 2
    return passthrough, controls


def normalize_configured_wc_controls(config, freeze, other=None):
    """Canonicalize authority-owned Combine controls and preserve other controls."""
    passthrough, controls = _collect_control_values(other)
    scientific = tuple(config.scientific_wcs)
    fixed = tuple(config.fixed_wcs)
    scientific_set = set(scientific)
    fixed_set = set(fixed)

    for value in controls["--redefineSignalPOIs"]:
        supplied = _parse_csv(value, "--redefineSignalPOIs")
        if len(supplied) != len(set(supplied)) or set(supplied) != scientific_set:
            raise ValueError("--redefineSignalPOIs contradicts the configured scientific WCs")

    expected_float = not freeze
    for value in controls["--floatOtherPOIs"]:
        normalized = value.strip().lower()
        if normalized not in {"0", "1", "false", "true"}:
            raise ValueError("--floatOtherPOIs must be true/false or 1/0")
        supplied_float = normalized in {"1", "true"}
        if supplied_float != expected_float:
            raise ValueError("--floatOtherPOIs contradicts the configured scan mode")

    unrelated_freeze = []
    seen_freeze = set()
    for value in controls["--freezeParameters"]:
        for parameter in _parse_csv(value, "--freezeParameters"):
            if _freeze_selector_matches_scientific(parameter, scientific_set):
                raise ValueError(
                    "--freezeParameters selector {} would freeze scientific WCs".format(
                        parameter
                    )
                )
            if parameter in fixed_set:
                continue
            if parameter not in seen_freeze:
                unrelated_freeze.append(parameter)
                seen_freeze.add(parameter)

    unrelated_assignments = []
    assignment_values = {}
    for value in controls["--setParameters"]:
        for assignment in _parse_csv(value, "--setParameters"):
            if "=" not in assignment:
                raise ValueError("invalid --setParameters assignment: {}".format(assignment))
            parameter, assigned_value = assignment.split("=", 1)
            parameter = parameter.strip()
            assigned_value = assigned_value.strip()
            if not parameter or not assigned_value:
                raise ValueError("invalid --setParameters assignment: {}".format(assignment))
            if parameter in fixed_set:
                if not _is_zero(assigned_value):
                    raise ValueError("fixed WC {} must equal zero".format(parameter))
                continue
            if parameter in scientific_set:
                if not _is_zero(assigned_value):
                    raise ValueError(
                        "nonzero scientific WC starting values are not canonical: {}".format(
                            parameter
                        )
                    )
                continue
            if parameter in assignment_values:
                if assignment_values[parameter] != assigned_value:
                    raise ValueError("conflicting assignments for {}".format(parameter))
                continue
            assignment_values[parameter] = assigned_value
            unrelated_assignments.append("{}={}".format(parameter, assigned_value))

    normalized = list(passthrough)
    normalized.extend(["--redefineSignalPOIs", ",".join(scientific)])
    if expected_float:
        normalized.extend(["--floatOtherPOIs", "1"])
    normalized.extend(
        [
            "--setParameters",
            ",".join(
                ["{}=0".format(wc) for wc in scientific + fixed]
                + unrelated_assignments
            ),
            "--freezeParameters",
            ",".join(list(fixed) + unrelated_freeze),
        ]
    )
    return normalized


def build_configured_grid_scan_command(
    fit_config_path,
    workspace,
    name=".test",
    freeze=False,
    scan_params=None,
    params_tracked=None,
    points=90000,
    other=None,
    track_error=False,
):
    """Build a configured CombineTool grid-scan argv without executing it."""
    config = load_fit_config(fit_config_path)
    scan_wcs = resolve_scan_wcs(config, scan_params)
    tracked = tuple(params_tracked or ())

    args = [
        "combineTool.py",
        "-d",
        str(workspace),
        "-M",
        "MultiDimFit",
        "--algo",
        "grid",
        "--cminPreScan",
        "--cminDefaultMinimizerStrategy=0",
        "--points",
        str(points),
    ]
    if name:
        args.extend(["-n", str(name)])
    for parameter in scan_wcs:
        args.extend(["-P", parameter])
    if tracked:
        args.extend(["--trackParameters", ",".join(tracked)])
    if track_error:
        args.extend(["--trackErrors", "rgx{.*}"])
    args.extend(normalize_configured_wc_controls(config, freeze, other))
    return args

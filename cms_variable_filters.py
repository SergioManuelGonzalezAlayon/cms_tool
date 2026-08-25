"""Shared parsing and metadata validation for generated-config filters."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
from typing import Any

from cms_protocol import VariableFilter


CONFIG_MANAGER_PATH = Path(__file__).resolve().parent / "tests" / "configs" / "config_manager.py"
_CONFIG_MANAGER_MODULE: Any | None = None


def parse_variable_filter(text: str) -> VariableFilter:
    """Parse one operator-supplied ``name=value`` variable filter."""

    name, separator, value = text.partition("=")
    if not separator:
        raise ValueError("variable filter must use name=value")
    return VariableFilter(name=name.strip(), value=value.strip())


def parse_variable_filters(values: list[str] | tuple[str, ...]) -> tuple[VariableFilter, ...]:
    return tuple(parse_variable_filter(value) for value in values)


def variable_options_from_parameters(parameters_path: Path) -> dict[str, tuple[str, ...]]:
    """Return accepted parameter values declared by one feature parameters.json."""

    data = json.loads(parameters_path.read_text(encoding="utf-8-sig"))
    steps = int(data.get("generator", {}).get("steps", 4))
    options: dict[str, tuple[str, ...]] = {}
    for parameter in data.get("parameters", []):
        if not isinstance(parameter, dict):
            continue
        name = str(parameter.get("name", "")).strip()
        if not name:
            continue
        values = _declared_parameter_values(parameter, steps)
        if values:
            options[name] = tuple(values)
    return options


def validate_variable_filters(
    parameters_path: Path,
    filters: tuple[VariableFilter, ...],
) -> None:
    """Validate filter names and exact values against parameters.json metadata."""

    options = variable_options_from_parameters(parameters_path)
    for variable_filter in filters:
        if variable_filter.name not in options:
            known = ", ".join(sorted(options)) or "none"
            raise ValueError(
                f"unknown variable '{variable_filter.name}' for {parameters_path.parent.name}; "
                f"available variables: {known}"
            )
        accepted_values = options[variable_filter.name]
        if variable_filter.value not in accepted_values:
            known_values = ", ".join(accepted_values)
            raise ValueError(
                f"unsupported value '{variable_filter.value}' for variable "
                f"'{variable_filter.name}'; accepted values: {known_values}"
            )


def config_matches_variable_filters(
    parameters_path: Path,
    config: dict[str, Any],
    filters: tuple[VariableFilter, ...],
) -> bool:
    """Return whether one generated config matches every requested filter."""

    if not filters:
        return True
    steps = _generator_steps(parameters_path)
    parameters = _parameters_by_name(parameters_path)
    validate_variable_filters(parameters_path, filters)
    return all(
        _config_matches_variable_filter(
            parameters[variable_filter.name],
            config,
            variable_filter,
            steps=steps,
        )
        for variable_filter in filters
    )


def variable_help_text(parameters_path: Path) -> str:
    """Build human help text from parameters.json without hard-coded values."""

    options = variable_options_from_parameters(parameters_path)
    if not options:
        return "No variable filters are declared for this feature."
    lines = []
    for name in sorted(options):
        lines.append(f"{name}: {', '.join(options[name])}")
    return "\n".join(lines)


def _parameters_by_name(parameters_path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(parameters_path.read_text(encoding="utf-8-sig"))
    parameters: dict[str, dict[str, Any]] = {}
    for parameter in data.get("parameters", []):
        if not isinstance(parameter, dict):
            continue
        name = str(parameter.get("name", "")).strip()
        if name:
            parameters[name] = parameter
    return parameters


def _config_matches_variable_filter(
    parameter: dict[str, Any],
    config: dict[str, Any],
    variable_filter: VariableFilter,
    *,
    steps: int,
) -> bool:
    for config_path in _parameter_config_paths(parameter):
        actual_value = _get_config_path(config, config_path)
        expected_values = _expected_config_values(
            parameter,
            config_path,
            variable_filter.value,
            steps=steps,
        )
        if any(actual_value == expected_value for expected_value in expected_values):
            return True
    return False


def _parameter_config_paths(parameter: dict[str, Any]) -> list[str]:
    paths = parameter.get("config_paths")
    if paths is None:
        path = str(parameter.get("config_path", "")).strip()
        return [path] if path else []
    if not isinstance(paths, list):
        return []
    return [str(path).strip() for path in paths if str(path).strip()]


def _expected_config_values(
    parameter: dict[str, Any],
    config_path: str,
    declared_value: str,
    *,
    steps: int,
) -> tuple[Any, ...]:
    values: list[Any] = [declared_value]
    for point in _parameter_points(parameter, steps=steps, include_default=True):
        if declared_value in {str(point.get("raw", "")), str(point.get("label", ""))}:
            _append_unique_any(values, point.get("runtime"))
    options = parameter.get("options", {})
    if isinstance(options, dict) and declared_value in options:
        _append_unique_any(values, options[declared_value])
    path_values = parameter.get("config_path_values", {})
    if isinstance(path_values, dict):
        mapping = path_values.get(config_path, {})
        if isinstance(mapping, dict) and declared_value in mapping:
            _append_unique_any(values, mapping[declared_value])
    return tuple(values)


def _get_config_path(config: dict[str, Any], config_path: str) -> Any:
    current: Any = config
    for part in config_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _declared_parameter_values(parameter: dict[str, Any], steps: int) -> list[str]:
    values: list[str] = []
    for point in _parameter_points(parameter, steps=steps, include_default=True):
        raw = str(point.get("raw", "")).strip()
        label = str(point.get("label", "")).strip()
        if raw:
            _append_unique(values, raw)
        if label:
            _append_unique(values, label)
    full_sweep_values = parameter.get("full_sweep_values", {})
    if isinstance(full_sweep_values, dict):
        for partition_values in full_sweep_values.values():
            if isinstance(partition_values, list):
                for value in partition_values:
                    _append_unique(values, str(value))
    return values


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _append_unique_any(values: list[Any], value: Any) -> None:
    if value not in values:
        values.append(value)


def _parameter_points(
    parameter: dict[str, Any],
    *,
    steps: int = 4,
    include_default: bool,
) -> list[dict[str, Any]]:
    if "range" not in parameter and "values" not in parameter:
        return []
    config_manager = _load_config_manager()
    return list(config_manager.points_for(parameter, steps, include_default=include_default))


def _load_config_manager() -> Any:
    global _CONFIG_MANAGER_MODULE
    if _CONFIG_MANAGER_MODULE is not None:
        return _CONFIG_MANAGER_MODULE
    spec = importlib.util.spec_from_file_location("cms_config_manager_for_filters", CONFIG_MANAGER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load config manager from {CONFIG_MANAGER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _CONFIG_MANAGER_MODULE = module
    return module


def _generator_steps(parameters_path: Path) -> int:
    data = json.loads(parameters_path.read_text(encoding="utf-8-sig"))
    return int(data.get("generator", {}).get("steps", 4))

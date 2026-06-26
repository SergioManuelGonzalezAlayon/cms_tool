import argparse
import json
import time
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from itertools import product
from pathlib import Path
import re
import sys


UNIT_SCALE = {
    "V": Decimal("1"),
    "dBm": Decimal("1"),
    "ms": Decimal("1"),
    "s": Decimal("1000"),
    "bytes": Decimal("1"),
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def cleanup_json_outputs(directory: Path, keep: set[str]) -> int:
    if not directory.exists():
        return 0
    removed = 0
    for path in directory.glob("*.json"):
        if path.name in keep:
            continue
        path.unlink()
        removed += 1
    return removed


def parse_quantity(text: str) -> tuple[Decimal, str]:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*", text)
    if not match:
        raise ValueError(f"Unsupported quantity: {text!r}")
    number = Decimal(match.group(1))
    unit = match.group(2)
    if unit not in UNIT_SCALE:
        raise ValueError(f"Unsupported unit: {unit!r}")
    return number * UNIT_SCALE[unit], unit


def choose_display_unit(value: Decimal, default_unit: str) -> tuple[Decimal, str]:
    if default_unit in {"V", "dBm", "bytes"}:
        return value / UNIT_SCALE[default_unit], default_unit
    if default_unit in {"ms", "s"}:
        if value >= Decimal("1000"):
            return value / Decimal("1000"), "s"
        return value, "ms"
    return value / UNIT_SCALE[default_unit], default_unit


def decimal_to_str(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def sanitize_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def set_path(config: dict, path: str, value) -> None:
    current = config
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def apply_value(config: dict, parameter: dict, raw_value) -> None:
    paths = parameter.get("config_paths")

    if paths is None:
        paths = [parameter["config_path"]]

    if parameter["name"] == "dcdc":
        value = parameter["options"][raw_value]
    else:
        value = raw_value

    for path in paths:
        set_path(config, path, value)


def quantity_to_runtime(value: Decimal, default_unit: str):
    display_value, display_unit = choose_display_unit(value, default_unit)
    if display_unit == "V":
        return float(display_value)
    if display_unit in {"dBm", "ms", "bytes"}:
        return int(display_value.to_integral_value(rounding=ROUND_HALF_UP))
    if display_unit == "s":
        ms_value = (display_value * Decimal("1000")).to_integral_value(rounding=ROUND_HALF_UP)
        return int(ms_value)
    return decimal_to_str(display_value)


def runtime_suffix(runtime_value, default_unit: str) -> str:
    if isinstance(runtime_value, float):
        number = decimal_to_str(Decimal(str(runtime_value))).replace(".", "p")
        return f"{number}{default_unit.lower()}"
    if isinstance(runtime_value, int):
        unit = "ms" if default_unit in {"ms", "s"} else default_unit.lower()
        return f"{runtime_value}{unit}"
    return sanitize_name(str(runtime_value))


def build_range_points(parameter: dict, steps: int, *, include_default: bool) -> list[dict]:
    default_value, default_unit = parse_quantity(parameter["default"])
    min_value, _ = parse_quantity(parameter["range"]["min"])
    max_value, _ = parse_quantity(parameter["range"]["max"])
    increment = (max_value - min_value) / Decimal(steps)
    points: list[dict] = []
    seen = set()
    for index in range(steps + 1):
        value = min_value + increment * Decimal(index)
        runtime_value = quantity_to_runtime(value, default_unit)
        label = runtime_suffix(runtime_value, default_unit)
        key = json.dumps(runtime_value, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        points.append({"runtime": runtime_value, "label": label, "raw": label})
    default_runtime = quantity_to_runtime(default_value, default_unit)
    if include_default:
        return points
    return [point for point in points if point["runtime"] != default_runtime]


def build_value_points(parameter: dict, *, include_default: bool) -> list[dict]:
    points: list[dict] = []
    for value in parameter["values"]:
        if not include_default and value == parameter["default"]:
            continue
        if isinstance(value, str):
            try:
                parsed_value, default_unit = parse_quantity(value)
            except ValueError:
                points.append({"runtime": value, "label": value, "raw": value})
                continue
            runtime_value = quantity_to_runtime(parsed_value, default_unit)
            points.append({"runtime": runtime_value, "label": runtime_suffix(runtime_value, default_unit), "raw": value})
            continue
        points.append({"runtime": value, "label": str(value), "raw": value})
    return points


def points_for(parameter: dict, steps: int, *, include_default: bool) -> list[dict]:
    if "range" in parameter:
        return build_range_points(parameter, steps, include_default=include_default)
    return build_value_points(parameter, include_default=include_default)


def one_factor_name(parameter: dict, point: dict) -> str:
    omitted = set(parameter.get("omit_value_in_name", []))
    if point["raw"] in omitted:
        return parameter["file_token"]
    return f"{parameter['file_token']}_{sanitize_name(point['label'])}"


def full_sweep_segment(parameter: dict, point: dict) -> str:
    omitted = set(parameter.get("omit_value_in_name", []))
    if point["raw"] in omitted:
        return ""
    if parameter["name"] == "dcdc":
        return parameter["file_token"]
    return sanitize_name(point["label"])


def partition_name(analysis: dict, combination: tuple, ordered_parameters: list[dict]) -> str | None:
    partition_parameter = analysis.get("partition_by")
    if not partition_parameter:
        return None
    partition_dirs = analysis.get("partition_dirs", {})
    for parameter, point in zip(ordered_parameters, combination):
        if parameter["name"] != partition_parameter:
            continue
        return partition_dirs.get(point["raw"])
    return None


def partition_directory(root_dir: Path, analysis: dict, combination: tuple, ordered_parameters: list[dict]) -> Path:
    target = partition_name(analysis, combination, ordered_parameters)
    if not target:
        return root_dir
    return root_dir / target


def combination_allowed(analysis: dict, combination: tuple, ordered_parameters: list[dict]) -> bool:
    partition = partition_name(analysis, combination, ordered_parameters)
    if not partition:
        return True
    for parameter, point in zip(ordered_parameters, combination):
        allowed_values = parameter.get("full_sweep_values", {}).get(partition)
        if allowed_values is None:
            continue
        if point["raw"] not in allowed_values:
            return False
    return True


def discover_parameter_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(root.rglob("parameters.json"))


def selected_modes(mode: str) -> set[str]:
    return {"one_factor", "full_sweep"} if mode == "both" else {mode}


def generate_config_matrix(config_dir: Path, *, modes: set[str]) -> dict:
    parameter_file = config_dir / "parameters.json"
    if not parameter_file.exists():
        raise FileNotFoundError(f"Missing parameter file: {parameter_file}")

    schema = load_json(parameter_file)
    default_config = load_json(config_dir / schema["generator"]["base_config"])
    output_dir = (config_dir / schema["generator"]["output_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    steps = int(schema["generator"].get("steps", 10))
    analyses = schema["generator"].get("analyses", {})
    parameter_map = {parameter["name"]: parameter for parameter in schema["parameters"]}

    one_factor_count = 0
    if "one_factor" in modes and analyses.get("one_factor", {}).get("enabled", True):
        cleanup_json_outputs(output_dir, {schema["generator"]["base_config"], parameter_file.name})
        for parameter in schema["parameters"]:
            points = points_for(parameter, steps, include_default=False)
            for point in points:
                config = deepcopy(default_config)
                apply_value(config, parameter, point["runtime"])
                stem = one_factor_name(parameter, point)
                config["test"] = {"name": stem}
                output_path = output_dir / f"{stem}.json"
                output_path.write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")
                one_factor_count += 1

    full_sweep_count = 0
    full_sweep = analyses.get("full_sweep", {})
    if "full_sweep" in modes and full_sweep.get("enabled"):
        sweep_dir = (config_dir / full_sweep.get("output_dir", "full_sweep")).resolve()
        sweep_dir.mkdir(parents=True, exist_ok=True)
        cleanup_json_outputs(sweep_dir, set())
        ordered_parameters = [parameter_map[name] for name in full_sweep["name_order"]]
        ordered_points = [points_for(parameter, steps, include_default=True) for parameter in ordered_parameters]
        if full_sweep.get("partition_by"):
            for partition_dir_name in full_sweep.get("partition_dirs", {}).values():
                partition_dir = sweep_dir / partition_dir_name
                partition_dir.mkdir(parents=True, exist_ok=True)
                cleanup_json_outputs(partition_dir, set())

        for combination in product(*ordered_points):
            if not combination_allowed(full_sweep, combination, ordered_parameters):
                continue
            config = deepcopy(default_config)
            segments = []
            for parameter, point in zip(ordered_parameters, combination):
                apply_value(config, parameter, point["runtime"])
                segment = full_sweep_segment(parameter, point)
                if segment:
                    segments.append(segment)
            stem = schema["generator"]["name_separator"].join(segments)
            config["test"] = {"name": stem}
            target_dir = partition_directory(sweep_dir, full_sweep, combination, ordered_parameters)
            target_dir.mkdir(parents=True, exist_ok=True)
            output_path = target_dir / f"{stem}.json"
            output_path.write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")
            full_sweep_count += 1

    return {
        "config_dir": str(config_dir),
        "one_factor_count": one_factor_count,
        "full_sweep_count": full_sweep_count,
    }


def clean_config_dir(config_dir: Path, *, modes: set[str]) -> dict:
    parameter_file = config_dir / "parameters.json"
    if not parameter_file.exists():
        raise FileNotFoundError(f"Missing parameter file: {parameter_file}")

    schema = load_json(parameter_file)
    removed = 0
    if "one_factor" in modes:
        output_dir = (config_dir / schema["generator"]["output_dir"]).resolve()
        removed += cleanup_json_outputs(output_dir, {schema["generator"]["base_config"], parameter_file.name})

    full_sweep = schema["generator"].get("analyses", {}).get("full_sweep", {})
    if "full_sweep" in modes and full_sweep.get("enabled"):
        sweep_dir = (config_dir / full_sweep.get("output_dir", "full_sweep")).resolve()
        removed += cleanup_json_outputs(sweep_dir, set())
        for partition_dir_name in full_sweep.get("partition_dirs", {}).values():
            removed += cleanup_json_outputs((sweep_dir / partition_dir_name).resolve(), set())

    return {"config_dir": str(config_dir), "removed_count": removed}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage BLE config matrices.")
    parser.add_argument(
        "action",
        nargs="?",
        choices=["generate", "clean", "rebuild"],
        default="generate",
        help="Config manager action. Defaults to generate.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        default=["tests/configs"],
        help="Config directories or parameters.json files to manage.",
    )
    parser.add_argument(
        "--mode",
        choices=["one_factor", "full_sweep", "both"],
        default="both",
        help="Which config subset to operate on. Defaults to both.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    parsed = parse_args(list(argv if argv is not None else sys.argv[1:]))
    targets = [Path(arg) for arg in parsed.targets]
    parameter_files: list[Path] = []
    for target in targets:
        parameter_files.extend(discover_parameter_files(target))

    unique_parameter_files = sorted({path.resolve() for path in parameter_files})
    if not unique_parameter_files:
        print("No parameters.json files found.")
        return 1

    modes = selected_modes(parsed.mode)
    started_at = time.perf_counter()
    total_one_factor = 0
    total_full_sweep = 0
    total_removed = 0

    if parsed.action in {"clean", "rebuild"}:
        for parameter_file in unique_parameter_files:
            summary = clean_config_dir(parameter_file.parent, modes=modes)
            total_removed += summary["removed_count"]
            print(f"{parameter_file.parent}: removed={summary['removed_count']}")

    if parsed.action in {"generate", "rebuild"}:
        for parameter_file in unique_parameter_files:
            summary = generate_config_matrix(parameter_file.parent, modes=modes)
            total_one_factor += summary["one_factor_count"]
            total_full_sweep += summary["full_sweep_count"]
            print(
                f"{parameter_file.parent}: "
                f"one_factor={summary['one_factor_count']} "
                f"full_sweep={summary['full_sweep_count']}"
            )

    print(
        "Config manager: "
        f"action={parsed.action} "
        f"parameter_sets={len(unique_parameter_files)} "
        f"mode={parsed.mode} "
        f"removed={total_removed} "
        f"one_factor={total_one_factor} "
        f"full_sweep={total_full_sweep} "
        f"elapsed_s={time.perf_counter() - started_at:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

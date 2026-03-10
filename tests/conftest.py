import json
import subprocess
import shutil
import logging
import time
import re
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from itertools import product
from pathlib import Path
from dataclasses import fields
import pytest

log = logging.getLogger(__name__)

UNIT_SCALE = {
    "V": Decimal("1"),
    "dBm": Decimal("1"),
    "ms": Decimal("1"),
    "s": Decimal("1000"),
    "bytes": Decimal("1"),
}

_GENERATED_CONFIG_DIRS = set()

def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--cfg",
        action="store",
        default=None,
        help="Config filename inside the test's config folder, e.g. non_conn_adv_200ms.json",
    )
    parser.addoption(
        "--provision-fw",
        action="store_true",
        help="Build+flash DUT and tester before tests",
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _cleanup_json_outputs(directory: Path, keep: set[str]) -> None:
    if not directory.exists():
        return
    for path in directory.glob("*.json"):
        if path.name in keep:
            continue
        path.unlink()


def _parse_quantity(text: str) -> tuple[Decimal, str]:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*", text)
    if not match:
        raise ValueError(f"Unsupported quantity: {text!r}")
    number = Decimal(match.group(1))
    unit = match.group(2)
    if unit not in UNIT_SCALE:
        raise ValueError(f"Unsupported unit: {unit!r}")
    return number * UNIT_SCALE[unit], unit


def _choose_display_unit(value: Decimal, default_unit: str) -> tuple[Decimal, str]:
    if default_unit in {"V", "dBm", "bytes"}:
        return value / UNIT_SCALE[default_unit], default_unit
    if default_unit in {"ms", "s"}:
        if value >= Decimal("1000"):
            return value / Decimal("1000"), "s"
        return value, "ms"
    return value / UNIT_SCALE[default_unit], default_unit


def _decimal_to_str(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _sanitize_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _set_path(config: dict, path: str, value) -> None:
    current = config
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _apply_parameter_value(config: dict, parameter: dict, raw_value) -> None:
    config_path = parameter["config_path"]
    if parameter["name"] == "dcdc":
        _set_path(config, config_path, parameter["options"][raw_value])
        return
    _set_path(config, config_path, raw_value)


def _quantity_to_runtime(value: Decimal, default_unit: str):
    display_value, display_unit = _choose_display_unit(value, default_unit)
    if display_unit == "V":
        return float(display_value)
    if display_unit in {"dBm", "ms", "bytes"}:
        return int(display_value.to_integral_value(rounding=ROUND_HALF_UP))
    if display_unit == "s":
        ms_value = (display_value * Decimal("1000")).to_integral_value(rounding=ROUND_HALF_UP)
        return int(ms_value)
    return _decimal_to_str(display_value)


def _runtime_suffix(runtime_value, default_unit: str) -> str:
    if isinstance(runtime_value, float):
        number = _decimal_to_str(Decimal(str(runtime_value))).replace(".", "p")
        return f"{number}{default_unit.lower()}"
    if isinstance(runtime_value, int):
        unit = "ms" if default_unit in {"ms", "s"} else default_unit.lower()
        return f"{runtime_value}{unit}"
    return _sanitize_name(str(runtime_value))


def _range_points(parameter: dict, steps: int, *, include_default: bool) -> list[dict]:
    default_value, default_unit = _parse_quantity(parameter["default"])
    min_value, _ = _parse_quantity(parameter["range"]["min"])
    max_value, _ = _parse_quantity(parameter["range"]["max"])
    increment = (max_value - min_value) / Decimal(steps)
    points = []
    seen = set()
    for index in range(steps + 1):
        value = min_value + increment * Decimal(index)
        runtime_value = _quantity_to_runtime(value, default_unit)
        label = _runtime_suffix(runtime_value, default_unit)
        key = json.dumps(runtime_value, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        points.append({"runtime": runtime_value, "label": label, "raw": label})
    default_runtime = _quantity_to_runtime(default_value, default_unit)
    if include_default:
        return points
    return [point for point in points if point["runtime"] != default_runtime]


def _value_points(parameter: dict, *, include_default: bool) -> list[dict]:
    points = []
    for value in parameter["values"]:
        if not include_default and value == parameter["default"]:
            continue
        if isinstance(value, str):
            try:
                parsed_value, default_unit = _parse_quantity(value)
            except ValueError:
                points.append({"runtime": value, "label": value, "raw": value})
                continue
            runtime_value = _quantity_to_runtime(parsed_value, default_unit)
            points.append({"runtime": runtime_value, "label": _runtime_suffix(runtime_value, default_unit), "raw": value})
            continue
        points.append({"runtime": value, "label": str(value), "raw": value})
    return points


def _parameter_points(parameter: dict, steps: int, *, include_default: bool) -> list[dict]:
    if "range" in parameter:
        return _range_points(parameter, steps, include_default=include_default)
    return _value_points(parameter, include_default=include_default)


def _one_factor_name(parameter: dict, point: dict) -> str:
    omitted = set(parameter.get("omit_value_in_name", []))
    if point["raw"] in omitted:
        return parameter["file_token"]
    return f"{parameter['file_token']}_{_sanitize_name(point['label'])}"


def _full_sweep_segment(parameter: dict, point: dict) -> str:
    omitted = set(parameter.get("omit_value_in_name", []))
    if point["raw"] in omitted:
        return ""
    if parameter["name"] == "dcdc":
        return parameter["file_token"]
    return _sanitize_name(point["label"])


def _partition_name(analysis: dict, combination: tuple, ordered_parameters: list[dict]) -> str | None:
    partition_parameter = analysis.get("partition_by")
    if not partition_parameter:
        return None
    partition_dirs = analysis.get("partition_dirs", {})
    for parameter, point in zip(ordered_parameters, combination):
        if parameter["name"] != partition_parameter:
            continue
        return partition_dirs.get(point["raw"])
    return None


def _partition_directory(root_dir: Path, analysis: dict, combination: tuple, ordered_parameters: list[dict]) -> Path:
    target = _partition_name(analysis, combination, ordered_parameters)
    if not target:
        return root_dir
    return root_dir / target


def _combination_allowed(analysis: dict, combination: tuple, ordered_parameters: list[dict]) -> bool:
    partition = _partition_name(analysis, combination, ordered_parameters)
    if not partition:
        return True
    for parameter, point in zip(ordered_parameters, combination):
        allowed_values = parameter.get("full_sweep_values", {}).get(partition)
        if allowed_values is None:
            continue
        if point["raw"] not in allowed_values:
            return False
    return True


def _generate_config_matrix(config_dir: Path) -> None:
    parameter_file = config_dir / "parameters.json"
    if not parameter_file.exists():
        return
    config_dir_key = str(config_dir.resolve())
    if config_dir_key in _GENERATED_CONFIG_DIRS:
        return

    schema = _load_json(parameter_file)
    default_config = _load_json(config_dir / schema["generator"]["base_config"])
    output_dir = (config_dir / schema["generator"]["output_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    steps = int(schema["generator"].get("steps", 10))
    analyses = schema["generator"].get("analyses", {})
    parameter_map = {parameter["name"]: parameter for parameter in schema["parameters"]}

    _cleanup_json_outputs(output_dir, {schema["generator"]["base_config"], parameter_file.name})

    if analyses.get("one_factor", {}).get("enabled", True):
        for parameter in schema["parameters"]:
            points = _parameter_points(parameter, steps, include_default=False)
            for point in points:
                config = deepcopy(default_config)
                _apply_parameter_value(config, parameter, point["runtime"])
                stem = _one_factor_name(parameter, point)
                config["test"] = {"name": stem}
                (output_dir / f"{stem}.json").write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")

    full_sweep = analyses.get("full_sweep", {})
    if full_sweep.get("enabled"):
        sweep_dir = (config_dir / full_sweep.get("output_dir", "full_sweep")).resolve()
        sweep_dir.mkdir(parents=True, exist_ok=True)
        _cleanup_json_outputs(sweep_dir, set())
        ordered_parameters = [parameter_map[name] for name in full_sweep["name_order"]]
        ordered_points = [_parameter_points(parameter, steps, include_default=True) for parameter in ordered_parameters]
        if full_sweep.get("partition_by"):
            for partition_name in full_sweep.get("partition_dirs", {}).values():
                partition_dir = sweep_dir / partition_name
                partition_dir.mkdir(parents=True, exist_ok=True)
                _cleanup_json_outputs(partition_dir, set())

        for combination in product(*ordered_points):
            if not _combination_allowed(full_sweep, combination, ordered_parameters):
                continue
            config = deepcopy(default_config)
            segments = []
            for parameter, point in zip(ordered_parameters, combination):
                _apply_parameter_value(config, parameter, point["runtime"])
                segment = _full_sweep_segment(parameter, point)
                if segment:
                    segments.append(segment)
            stem = schema["generator"]["name_separator"].join(segments)
            config["test"] = {"name": stem}
            target_dir = _partition_directory(sweep_dir, full_sweep, combination, ordered_parameters)
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / f"{stem}.json").write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")

    _GENERATED_CONFIG_DIRS.add(config_dir_key)


def _test_config_dir(request: pytest.FixtureRequest) -> Path:
    """
    Map nodeid like 'tests/ble.py::test_beacon' -> 'tests/configs/ble/beacon'
    """
    file_part, test_name = request.node.nodeid.split("::", 1)
    test_module = Path(file_part).stem  # 'ble'
    feature = test_name.removeprefix("test_")  # 'beacon'
    return Path("tests") / "configs" / test_module / feature


def _test_config_path(request: pytest.FixtureRequest) -> Path:
    cfg_name = request.config.getoption("--cfg")
    cfg_dir = _test_config_dir(request)

    if cfg_name:
        return cfg_dir / cfg_name
    return cfg_dir / "default.json"

def dataclass_from_dict(cls, data: dict):
    valid = {f.name for f in fields(cls)}
    unknown = set(data.keys()) - valid
    if unknown:
        raise ValueError(f"Unknown fields for {cls.__name__}: {sorted(unknown)}")
    return cls(**data)

@pytest.fixture
def Config(request: pytest.FixtureRequest) -> dict:
    _generate_config_matrix(_test_config_dir(request))
    cfg_path = _test_config_path(request)

    if not cfg_path.exists():
        pytest.fail(
            f"Missing config for {request.node.nodeid}\n"
            f"Looked for: {cfg_path}\n"
            f"Create default.json or pass --cfg <file.json> (within {cfg_path.parent})."
        )

    return json.loads(cfg_path.read_text(encoding="utf-8-sig"))


@pytest.fixture(autouse=True)
def JoulescopeCaptureContext(request: pytest.FixtureRequest) -> None:
    if "JoulescopeDev" not in request.fixturenames:
        return

    js = request.getfixturevalue("JoulescopeDev")
    js.capture_test_name = request.node.name.removeprefix("test_")
    cfg_opt = request.config.getoption("--cfg")
    js.capture_config_name = Path(cfg_opt).name if cfg_opt else "default.json"

def _run(cmd, cwd=None):
    exe = shutil.which(cmd[0])
    if exe is None:
        pytest.fail(
            f"Required tool '{cmd[0]}' not found on PATH. "
            "Ensure Simplicity Studio CLI tools and CMake are installed and available."
        )

    resolved_cmd = [exe, *cmd[1:]]
    subprocess.run(resolved_cmd, cwd=cwd, check=True, text=True)


def _read_dev_config(path: Path = Path("dev_config.json")) -> dict:
    if not path.exists():
        pytest.fail(f"Missing {path}. Required for probe serial fallback.")
    return json.loads(path.read_text(encoding="utf-8-sig"))

def _upgrade_generated_slcp(slcp: Path, out_dir: Path) -> Path:
    generated_slcp = out_dir / slcp.name
    shutil.copy2(slcp, generated_slcp)
    _run(["slc", "upgrade", "-p", generated_slcp.name], cwd=out_dir)
    return generated_slcp


def _build_slcp(slcp: Path, out_dir: Path, project_name: str, board: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("FW build start: project=%s slcp=%s out=%s board=%s", project_name, slcp, out_dir, board)
    t0 = time.monotonic()

    _run(["slc", "generate", str(slcp), "-d", str(out_dir), f"-name={project_name}", "--with", board])
    log.info("FW generate done: project=%s", project_name)
    generated_slcp = _upgrade_generated_slcp(slcp, out_dir)
    log.info("FW upgrade done: project=%s slcp=%s", project_name, generated_slcp)
    _run(["cmake", "--workflow", "--preset", "project"], cwd=out_dir / "cmake_gcc")
    log.info("FW compile done: project=%s", project_name)

    candidates = list((out_dir / "cmake_gcc").rglob("*.s37")) + list((out_dir / "cmake_gcc").rglob("*.hex"))
    if not candidates:
        raise RuntimeError(f"No firmware image found under {out_dir}")
    log.info("FW build complete: project=%s image=%s elapsed=%.1fs", project_name, candidates[0], time.monotonic() - t0)
    return candidates[0]


def _flash(image: Path, probe_sn: str):
    _run(["commander", "flash", str(image), "--serialno", probe_sn])


_PROVISIONED_CONFIGS = set()


@pytest.fixture(autouse=True)
def ProvisionFirmware(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--provision-fw"):
        return

    if "Config" not in request.fixturenames:
        return

    cfg_path = str(_test_config_path(request).resolve())
    if cfg_path in _PROVISIONED_CONFIGS:
        return

    config = request.getfixturevalue("Config")
    fw = config.get("firmware", {})
    required = (
        "dut_slcp",
        "tester_slcp",
        "board",
    )
    missing = [key for key in required if key not in fw]
    if missing:
        pytest.fail("Missing 'firmware' fields in test config: " + ", ".join(missing))

    dev_cfg = _read_dev_config()
    build_root_str = fw.get("build_root") or dev_cfg.get("firmware", {}).get("build_root")
    if not build_root_str:
        pytest.fail(
            "Missing build root. Set dev_config.json firmware.build_root "
            "or provide firmware.build_root in test config."
        )
    build_root = Path(build_root_str)
    dut_probe_sn = fw.get("dut_probe_sn") or dev_cfg.get("dut", {}).get("probe_sn")
    tester_probe_sn = fw.get("tester_probe_sn") or dev_cfg.get("tester", {}).get("probe_sn")
    if not dut_probe_sn or not tester_probe_sn:
        pytest.fail(
            "Missing probe serials. Set dev_config.json dut.probe_sn and tester.probe_sn "
            "or provide firmware.dut_probe_sn/tester_probe_sn in test config."
        )

    dut_image = _build_slcp(
        slcp=Path(fw["dut_slcp"]),
        out_dir=build_root / "dut",
        project_name="ncp_ble_dut",
        board=fw["board"],
    )
    tester_image = _build_slcp(
        slcp=Path(fw["tester_slcp"]),
        out_dir=build_root / "tester",
        project_name="ncp_ble_tester",
        board=fw["board"],
    )

    _flash(dut_image, dut_probe_sn)
    _flash(tester_image, tester_probe_sn)

    _PROVISIONED_CONFIGS.add(cfg_path)

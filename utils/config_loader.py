import json
import os
import re
from decimal import Decimal
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_NAME = "dev_config.json"
BUILD_CONFIG_NAME = "cms_client_config.json"
RUNTIME_CONFIG_NAME = "cms_bench_runtime.json"
DEFAULT_CONFIG_ENV = "CURRENT_MEASUREMENT_DEV_CONFIG_FILE"
BUILD_CONFIG_ENV = "CURRENT_MEASUREMENT_CMS_CLIENT_CONFIG_FILE"
RUNTIME_CONFIG_ENV = "CURRENT_MEASUREMENT_CMS_BENCH_RUNTIME_FILE"
PAWR_PHY_BYTE_TIME_MS = {"1m": 0.008, "2m": 0.004}


def _read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    return _load_json(path)


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_explicit_path(env_name: str) -> Path | None:
    configured = os.getenv(env_name)
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Configured path in {env_name} does not exist: {path}")
    return path


def _default_path(filename: str) -> Path:
    return REPO_ROOT / filename

def _load_json(path: str | Path) -> dict:
    raw = Path(path).read_text(encoding="utf-8-sig")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", raw))


def load_test_config(path: str | Path) -> dict:
    config = _load_json(path)
    if not isinstance(config, dict):
        raise ValueError(f"test config must be a JSON object: {path}")
    for section in ("test", "advertiser", "scanner", "connection", "pawr", "firmware", "hw"):
        if section in config and not isinstance(config[section], dict):
            raise ValueError(f"test config section must be an object: {section}")
    return config


def validate_test_config_data(config: dict) -> None:
    if not isinstance(config, dict):
        raise ValueError("test config must be a JSON object")
    for section in ("test", "advertiser", "scanner", "connection", "pawr", "firmware", "hw"):
        if section in config and not isinstance(config[section], dict):
            raise ValueError(f"test config section must be an object: {section}")
    validate_ble_test_config(config)


def validate_test_config_set_data(configs) -> None:
    """Validate a named batch of custom test configs before submission or execution."""
    if not isinstance(configs, list) or not configs:
        raise ValueError("custom config set must contain at least one config")

    names = set()
    errors = []
    for index, item in enumerate(configs):
        if not isinstance(item, dict):
            errors.append(f"entry {index + 1} must be an object")
            continue
        name = item.get("name")
        config = item.get("config")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"entry {index + 1} needs a name")
            continue
        relative_name = PurePosixPath(name.replace("\\", "/"))
        if (
            relative_name.is_absolute()
            or any(part in {"", ".", ".."} for part in relative_name.parts)
            or relative_name.suffix.lower() != ".json"
            or ":" in name
        ):
            errors.append(f"entry has an invalid name: {name!r}")
            continue
        normalized_name = relative_name.as_posix().casefold()
        if normalized_name in names:
            errors.append(f"duplicate config name: {name}")
            continue
        names.add(normalized_name)
        try:
            validate_test_config_data(config)
        except (TypeError, ValueError) as exc:
            errors.append(f"config is invalid ({name}): {exc}")
    if errors:
        raise ValueError("custom config set validation failed: " + "; ".join(errors))


def pawr_phy_byte_time_ms(phy) -> float:
    if isinstance(phy, int) and phy in {1, 2}:
        return PAWR_PHY_BYTE_TIME_MS[f"{phy}m"]
    if isinstance(phy, str):
        normalized = phy.strip().lower().replace(" ", "")
        if normalized in PAWR_PHY_BYTE_TIME_MS:
            return PAWR_PHY_BYTE_TIME_MS[normalized]
    raise ValueError(f"Unsupported PAWR PHY: {phy!r}")


def is_pawr_config_valid(
    response_slot_number, subevent_number, subevent_interval, pawr_interval,
    response_slot_delay, response_slot_spacing, payload_size, phy, t_ifs=0.150,
) -> bool:
    overhead_bytes = 10  # Preamble, access address, LL header, and CRC.
    spacing_ms = Decimal(response_slot_spacing) * Decimal("0.125")
    subevent_ms = Decimal(subevent_interval) * Decimal("1.25")
    interval_ms = Decimal(str(pawr_interval))
    delay_ms = Decimal(response_slot_delay) * Decimal("1.25")
    t_ifs_ms = Decimal(str(t_ifs))
    byte_time_ms = Decimal(str(pawr_phy_byte_time_ms(phy)))

    if Decimal(overhead_bytes + payload_size) * byte_time_ms + t_ifs_ms > spacing_ms:
        return False

    used_airtime_per_subevent = (
        Decimal(response_slot_number) * (spacing_ms + t_ifs_ms) + delay_ms + t_ifs_ms
    )
    if used_airtime_per_subevent > subevent_ms:
        return False

    return Decimal(subevent_number) * subevent_ms <= interval_ms


def is_generated_pawr_config_valid(config: dict) -> bool:
    pawr = config.get("pawr")
    if not pawr:
        return True
    advertiser = config.get("advertiser", {})
    try:
        return is_pawr_config_valid(
            int(pawr["response_slots"]), int(pawr["num_subevents"]),
            int(pawr["subevent_interval"]), int(pawr["interval"]),
            int(pawr["response_slot_delay"]), int(pawr["response_slot_spacing"]),
            max(int(advertiser.get("payload", 0)), int(pawr.get("rx_payload_length", 0))),
            advertiser.get("secondary_phy", advertiser.get("phy", "1m")),
        )
    except (KeyError, TypeError, ValueError):
        return False


def validate_ble_test_config(config: dict) -> None:
    from utils.bgapi_utils import (
        AdvertiserConfig, ConnectionParameters, PAWRConfig, ScannerConfig,
        normalize_advertiser_config, normalize_connection_parameters,
        normalize_scanner_config,
    )

    if not isinstance(config, dict):
        raise ValueError("test config must be a JSON object")
    for name, config_type, normalize in (
        ("advertiser", AdvertiserConfig, normalize_advertiser_config),
        ("scanner", ScannerConfig, normalize_scanner_config),
        ("connection", ConnectionParameters, normalize_connection_parameters),
    ):
        if name in config:
            try:
                normalize(config_type(**config[name]))
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid {name} BLE parameters: {exc}") from exc
    if "pawr" in config:
        try:
            pawr = PAWRConfig(**config["pawr"])
            if any(type(value) is not int for value in vars(pawr).values()):
                raise ValueError("PAwR parameters must be integers")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid PAwR parameters: {exc}") from exc
        if not is_generated_pawr_config_valid(config):
            raise ValueError("PAwR timing and payload parameters are incompatible")


def effective_dev_config(*, runtime_path=None, metadata_path=None) -> dict:
    if not runtime_path and not metadata_path:
        return load_effective_config(role="runtime")

    runtime = _load_json(runtime_path) if runtime_path else {}
    metadata = _load_json(metadata_path) if metadata_path else {}

    devices = runtime.get("devices", {})
    bench = runtime.get("bench", {})
    dut = runtime.get("dut", {})
    tester = runtime.get("tester", {})
    tools = runtime.get("tools", {})
    ble = runtime.get("ble", {})
    sdk = ble.get("sdk", {}) if isinstance(ble, dict) else {}
    joulescope = runtime.get("joulescope", runtime.get("Joulescope", {}))
    fw_metadata = metadata.get("firmware", {})

    return {
        "dut": {
            "serial_port": dut.get("serial_port") or metadata.get("dut", {}).get("serial_port"),
            "probe_sn": dut.get("probe_sn") or metadata.get("dut", {}).get("probe_sn"),
        },
        "tester": {
            "serial_port": tester.get("serial_port") or metadata.get("tester", {}).get("probe_sn"),
            "probe_sn": tester.get("probe_sn") or metadata.get("tester", {}).get("probe_sn"),
        },
        "tools": {
            "commander": tools.get("commander") or metadata.get("tools", {}).get("commander"),
            "jlink_library": tools.get("jlink_library") or metadata.get("tools", {}).get("jlink_library"),
        },
        "ble": {
            "xapi": metadata.get("ble", {}).get("xapi") or ble.get("xapi"),
        },
        "joulescope": {
            "id": (
                joulescope.get("id")
                or metadata.get("joulescope", {}).get("id")
                or metadata.get("Joulescope", {}).get("id")
            ),
            "model": str(
                joulescope.get("model")
                or metadata.get("joulescope", {}).get("model")
                or metadata.get("Joulescope", {}).get("model")
                or ""
            ).strip().lower(),
        },
        "bench": {
            "bench_id": bench.get("bench_id"),
            "profile": bench.get("profile"),
        },
        "firmware": {
            "build_root": fw_metadata.get("build_root"),
            "provision_state_root": fw_metadata.get("provision_state_root"),
            "bootloader_image": fw_metadata.get("bootloader_image"),
            "bootloader_image_sha256": fw_metadata.get("bootloader_image_sha256"),
            "board": fw_metadata.get("board") or devices.get("board"),
            "brev": fw_metadata.get("brev") or devices.get("board_rev"),
            "board_rev": fw_metadata.get("board_rev") or fw_metadata.get("brev") or devices.get("board_rev"),
            "board_revision": (
                fw_metadata.get("board_revision")
                or fw_metadata.get("board_rev")
                or fw_metadata.get("brev")
                or devices.get("board_revision")
                or devices.get("board_rev")
            ),
            "part_number": fw_metadata.get("part_number") or devices.get("part_number") or devices.get("device"),
            "serial_number": fw_metadata.get("serial_number") or devices.get("serial_number"),
            "device": fw_metadata.get("device") or devices.get("device"),
            "sisdk": fw_metadata.get("sisdk"),
            "sisdk_name": fw_metadata.get("sisdk_name") or sdk.get("name"),
            "sisdk_source": fw_metadata.get("sisdk_source") or sdk.get("source"),
            "sisdk_version": fw_metadata.get("sisdk_version") or sdk.get("version"),
            "sisdk_commit": fw_metadata.get("sisdk_commit") or sdk.get("commit"),
            "sisdk_hash": fw_metadata.get("sisdk_hash") or sdk.get("hash"),
            "xapi_file_hash": fw_metadata.get("xapi_file_hash") or sdk.get("xapi_sha256"),
        },
        "vcom": metadata.get("vcom", runtime.get("vcom", {})),
    }

def get_effective_config_paths(*, role: str | None = None) -> list[Path]:
    paths: list[Path] = []

    base_path = _resolve_explicit_path(DEFAULT_CONFIG_ENV) or _default_path(DEFAULT_CONFIG_NAME)
    if base_path.exists():
        paths.append(base_path)

    if role == "build":
        overlay_path = _resolve_explicit_path(BUILD_CONFIG_ENV) or _default_path(BUILD_CONFIG_NAME)
        if overlay_path.exists():
            paths.append(overlay_path)
    elif role == "runtime":
        overlay_path = _resolve_explicit_path(RUNTIME_CONFIG_ENV) or _default_path(RUNTIME_CONFIG_NAME)
        if overlay_path.exists():
            paths.append(overlay_path)

    return paths


def load_effective_config(*, role: str | None = None) -> dict:
    config: dict = {}
    for path in get_effective_config_paths(role=role):
        config = _deep_merge(config, _read_json_file(path))
    return config

import json
import os
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_NAME = "dev_config.json"
BUILD_CONFIG_NAME = "cms_client_config.json"
RUNTIME_CONFIG_NAME = "cms_bench_runtime.json"
DEFAULT_CONFIG_ENV = "CURRENT_MEASUREMENT_DEV_CONFIG_FILE"
BUILD_CONFIG_ENV = "CURRENT_MEASUREMENT_CMS_CLIENT_CONFIG_FILE"
RUNTIME_CONFIG_ENV = "CURRENT_MEASUREMENT_CMS_BENCH_RUNTIME_FILE"


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

def effective_dev_config(*, runtime_path=None, metadata_path=None) -> dict:
    if not runtime_path and not metadata_path:
        return load_effective_config(role="runtime")

    runtime = _load_json(runtime_path) if runtime_path else {}
    metadata = _load_json(metadata_path) if metadata_path else {}

    devices = runtime.get("devices", {})
    dut = runtime.get("dut", {})
    tester = runtime.get("tester", {})
    tools = runtime.get("tools", {})
    ble = runtime.get("ble", {})
    sdk = ble.get("sdk", {}) if isinstance(ble, dict) else {}
    js = runtime.get("js", {})
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
        "js": {
            "usb_id": js.get("usb_id") or metadata.get("js", {}).get("usb_id"),
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

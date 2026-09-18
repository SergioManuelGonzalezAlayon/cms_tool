import json
import platform
import tempfile
from pathlib import Path

import pytest

from utils.config_loader import effective_dev_config
from utils.wpk import (
    VcomGpio,
    _jlink_device_candidates,
    _resolve_jlink_library_path,
    parse_board_vcom_gpio,
    parse_vcom_gpio_header,
    setDebugMode,
    setVCOM,
)


def test_parse_brd4182a_vcom_gpio_from_sisdk_2025_12_2() -> None:
    sdk_path = _installed_simplicity_sdk_path("2025.12.2")

    assert parse_board_vcom_gpio(sdk_path, "brd4182a") == VcomGpio(port="B", pin=4)


def test_parse_vcom_gpio_header_requires_expected_defines() -> None:
    with pytest.raises(ValueError, match="missing SL_VCOM_ENABLE_PORT"):
        parse_vcom_gpio_header("#define SL_VCOM_ENABLE_PIN 4\n")


def test_resolve_jlink_library_from_linux_commander_tooling(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("utils.wpk.sys.platform", "linux")
    tools_dir = tmp_path / "tools" / "commander-cli"
    commander = tools_dir / "commander-cli"
    jlink_library = tools_dir / "libjlinkarm.so.8"
    tools_dir.mkdir(parents=True)
    commander.write_text("", encoding="utf-8")
    jlink_library.write_text("", encoding="utf-8")

    assert _resolve_jlink_library_path({"tools": {"commander": str(commander)}}) == jlink_library


def test_resolve_jlink_library_from_explicit_tool_config(tmp_path: Path) -> None:
    jlink_library = tmp_path / "libjlinkarm.so"
    jlink_library.write_text("", encoding="utf-8")

    assert _resolve_jlink_library_path({"tools": {"jlink_library": str(jlink_library)}}) == jlink_library


def test_jlink_device_candidates_try_debugger_name_before_package_suffix() -> None:
    assert _jlink_device_candidates(
        {
            "device": "EFR32MG22C224F512IM40-C",
            "svd_device": "EFR32MG22A224F512IM40",
        },
        {},
    ) == (
        "EFR32MG22C224F512IM40",
        "EFR32MG22C224F512IM40-C",
        "EFR32MG22A224F512IM40",
    )


def test_bench_wpk_vcom_and_debug_mode_round_trip(request: pytest.FixtureRequest) -> None:
    config = _effective_bench_wpk_config(request)
    vcom = config.get("vcom")
    if not isinstance(vcom, dict) or not vcom or vcom.get("enabled") is False:
        pytest.skip("CMS VCOM metadata is not available")

    missing = [
        name
        for name, value in (
            ("dut.probe_sn", config.get("dut", {}).get("probe_sn")),
            ("tools.commander", config.get("tools", {}).get("commander")),
            ("vcom.gpio", vcom.get("gpio")),
        )
        if not value
    ]
    if missing:
        pytest.fail("Missing CMS WPK runtime metadata: " + ", ".join(missing))

    vcom_disabled = False
    debug_off = False
    restore_errors: list[str] = []
    try:
        setVCOM(config, False)
        vcom_disabled = True
        setDebugMode(config, "OFF")
        debug_off = True
    finally:
        if debug_off:
            try:
                setDebugMode(config, "MCU")
            except Exception as exc:
                restore_errors.append(f"debug MCU restore failed: {exc}")
        if vcom_disabled:
            try:
                setVCOM(config, True)
            except Exception as exc:
                restore_errors.append(f"VCOM restore failed: {exc}")

    if restore_errors:
        pytest.fail("; ".join(restore_errors))


def _effective_bench_wpk_config(request: pytest.FixtureRequest) -> dict:
    runtime_path = request.config.getoption("--cms-runtime-config") or "cms_bench_runtime.json"
    metadata_path = request.config.getoption("--cms-fw-metadata")
    if metadata_path:
        return effective_dev_config(runtime_path=runtime_path, metadata_path=metadata_path)

    config = effective_dev_config(runtime_path=runtime_path)
    request_path = _cmsd_staged_request_path()
    if not request_path.is_file() or request_path.stat().st_size == 0:
        pytest.skip(
            "CMSD staged firmware request is not available. "
            "Start cmsd and run cms SEND_FW, or run under CMSD pytest orchestration."
        )

    request_data = json.loads(request_path.read_text(encoding="utf-8-sig"))
    firmware = request_data.get("firmware", request_data)
    if not isinstance(firmware, dict):
        pytest.fail(f"CMSD staged firmware request has invalid firmware data: {request_path}")

    config["vcom"] = firmware.get("vcom", {})
    if firmware.get("device") and not config.get("firmware", {}).get("device"):
        config.setdefault("firmware", {})["device"] = firmware.get("device")
    return config


def _cmsd_staged_request_path() -> Path:
    root = Path("/dev/shm/cmsd") if platform.system() == "Linux" else Path(tempfile.gettempdir()) / "cmsd"
    return root / "measurement_request.json"


def _installed_simplicity_sdk_path(version: str) -> Path:
    catalog_path = Path.home() / ".silabs" / "sdks.json"
    if not catalog_path.is_file():
        pytest.skip(f"Silicon Labs SDK catalog is not installed: {catalog_path}")

    catalog = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
    for sdk in catalog:
        if str(sdk.get("id")) != version:
            continue
        for extension in sdk.get("extensions", []):
            if (
                str(extension.get("id")) == "simplicity-sdk"
                and str(extension.get("version")) == version
            ):
                sdk_path = Path(str(extension.get("path", "")))
                if sdk_path.is_dir():
                    return sdk_path
                pytest.skip(f"Simplicity SDK {version} path is not installed: {sdk_path}")

    pytest.skip(f"Simplicity SDK {version} is not listed in {catalog_path}")

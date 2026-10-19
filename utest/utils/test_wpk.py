import json
from pathlib import Path

import pytest

from utils.wpk import (
    VcomGpio,
    _jlink_device_candidates,
    _resolve_jlink_library_path,
    parse_board_vcom_gpio,
    parse_vcom_gpio_header,
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

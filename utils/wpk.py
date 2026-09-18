"""WPK helpers for board metadata and bench-side control.

This module is the home for WPK-related helpers used by CMS. Build-time helpers
parse board headers so the CMS Client can send resolved hardware-control data
to the CMS Bench. Runtime helpers use that metadata to control DUT WPK state.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_VCOM_PORT_DEFINE = "SL_VCOM_ENABLE_PORT"
_VCOM_PIN_DEFINE = "SL_VCOM_ENABLE_PIN"
_DEFINE_RE = re.compile(r"^\s*#\s*define\s+(?P<name>\w+)\s+(?P<value>\S+)")
_GPIO_PORT_RE = re.compile(r"^SL_GPIO_PORT_(?P<port>[A-Za-z])$")
_DEVICE_PACKAGE_SUFFIX_RE = re.compile(r"-[A-Z]+$")
log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VcomGpio:
    """GPIO identifier for the board VCOM enable signal."""

    port: str
    pin: int


def vcom_gpio_header_path(
    sisdk_package_path: str | Path,
    board: str,
    adapter_board: str = "brd4002a",
) -> Path:
    """Return the SiSDK header path declaring VCOM for a radio board on a WPK."""

    radio_board_id = _normalize_radio_board_id(board)
    adapter_id = adapter_board.strip().lower()
    if not adapter_id:
        raise ValueError("adapter_board is required")
    return (
        Path(sisdk_package_path)
        / "boards"
        / "hardware"
        / "board"
        / "config"
        / f"{radio_board_id}_{adapter_id}"
        / "btl_uart_driver_cfg.h"
    )


def parse_vcom_gpio_header(header: str) -> VcomGpio:
    """Parse VCOM enable GPIO metadata from a board config header."""

    defines = _parse_c_defines(header)
    try:
        raw_port = defines[_VCOM_PORT_DEFINE]
    except KeyError as exc:
        raise ValueError(f"missing {_VCOM_PORT_DEFINE} define") from exc
    try:
        raw_pin = defines[_VCOM_PIN_DEFINE]
    except KeyError as exc:
        raise ValueError(f"missing {_VCOM_PIN_DEFINE} define") from exc

    return VcomGpio(
        port=_normalize_gpio_port(raw_port),
        pin=_parse_gpio_pin(raw_pin),
    )


def parse_vcom_gpio_header_file(path: str | Path) -> VcomGpio:
    """Read and parse VCOM enable GPIO metadata from a board config header."""

    header_path = Path(path)
    return parse_vcom_gpio_header(header_path.read_text(encoding="utf-8-sig"))


def parse_board_vcom_gpio(
    sisdk_package_path: str | Path,
    board: str,
    adapter_board: str = "brd4002a",
) -> VcomGpio:
    """Parse VCOM GPIO metadata for a board from an unpacked SiSDK package."""

    return parse_vcom_gpio_header_file(
        vcom_gpio_header_path(sisdk_package_path, board, adapter_board)
    )


def setVCOM(config: dict[str, Any], enabled: bool) -> None:
    """Enable or disable the DUT WPK VCOM GPIO from resolved CMS metadata."""

    vcom = _vcom_config(config)
    if vcom is None:
        return
    gpio = _required_dict(vcom.get("gpio"), "vcom.gpio")
    dut_probe_serial = _required_text(
        _required_dict(config.get("dut"), "dut").get("probe_sn"),
        "dut.probe_sn",
    )
    firmware = config.get("firmware", {})
    device_candidates = _jlink_device_candidates(vcom, firmware if isinstance(firmware, dict) else {})
    pin_mask = _required_u32(gpio.get("pin_mask"), "vcom.gpio.pin_mask")
    dout_address = _optional_u32(gpio.get("dout_address"))
    dout_set_address = _optional_u32(gpio.get("dout_set_address"))
    dout_clear_address = _optional_u32(gpio.get("dout_clear_address"))

    if dout_address is None:
        if enabled and dout_set_address is None:
            raise RuntimeError("vcom.gpio requires dout_address or dout_set_address to enable VCOM")
        if not enabled and dout_clear_address is None:
            raise RuntimeError("vcom.gpio requires dout_address or dout_clear_address to disable VCOM")

    jlink = _jlink(config)
    opened = False
    try:
        jlink.open(serial_no=int(dut_probe_serial))
        opened = True
        _select_swd(jlink)
        _connect_jlink(jlink, device_candidates)
        _configure_vcom_pin_mode(jlink, gpio)

        if dout_address is not None:
            before = _read_u32(jlink, dout_address)
            after = before | pin_mask if enabled else before & ~pin_mask
            if after != before:
                _write_u32(jlink, dout_address, after)
        elif enabled:
            _write_u32(jlink, dout_set_address, pin_mask)
        else:
            _write_u32(jlink, dout_clear_address, pin_mask)
        log.info("VCOM %s", "enabled" if enabled else "disabled")
    finally:
        if opened:
            jlink.close()


def setDebugMode(config: dict[str, Any], mode: str) -> None:
    """Set DUT adapter debug mode through commander."""

    if _vcom_config(config) is None:
        return
    normalized_mode = mode.strip().upper()
    if normalized_mode not in {"OFF", "MCU"}:
        raise RuntimeError(f"unsupported DUT debug mode: {mode}")

    dut_probe_serial = _required_text(
        _required_dict(config.get("dut"), "dut").get("probe_sn"),
        "dut.probe_sn",
    )
    commander = _required_text(
        _required_dict(config.get("tools"), "tools").get("commander"),
        "tools.commander",
    )
    command = [
        commander,
        "adapter",
        "dbgmode",
        normalized_mode,
        "--serialno",
        dut_probe_serial,
    ]
    result = subprocess.run(command, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        detail = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        raise RuntimeError(
            f"Unable to set DUT debug mode {normalized_mode}"
            + (f": {detail}" if detail else "")
        )
    log.info("Debug %s", normalized_mode)


def _normalize_radio_board_id(board: str) -> str:
    radio_board_id = board.strip().lower()
    if not radio_board_id:
        raise ValueError("board is required")
    return radio_board_id.split("_", 1)[0]


def _vcom_config(config: dict[str, Any]) -> dict[str, Any] | None:
    vcom = config.get("vcom")
    if not isinstance(vcom, dict) or not vcom:
        return None
    if vcom.get("enabled") is False:
        return None
    return vcom


def _jlink(config: dict[str, Any]) -> Any:
    try:
        import pylink
    except ImportError as exc:
        raise RuntimeError("pylink is required to control DUT VCOM") from exc

    library_path = _resolve_jlink_library_path(config)
    try:
        if library_path is not None:
            return pylink.JLink(
                lib=pylink.Library(dllpath=str(library_path), use_tmpcpy=False),
                log=_ignore_jlink_message,
                detailed_log=_ignore_jlink_message,
                warn=_ignore_jlink_message,
            )
        return pylink.JLink(
            log=_ignore_jlink_message,
            detailed_log=_ignore_jlink_message,
            warn=_ignore_jlink_message,
        )
    except (OSError, TypeError) as exc:
        hint = (
            "Configure tools.jlink_library or tools.commander so CMS can find "
            "the SEGGER J-Link native library."
        )
        if library_path is not None:
            hint = f"Tried J-Link library: {library_path}. {hint}"
        raise RuntimeError(f"Unable to load SEGGER J-Link library. {hint}") from exc


def _ignore_jlink_message(_message: Any) -> None:
    return


def _select_swd(jlink: Any) -> None:
    try:
        import pylink

        jlink.set_tif(pylink.JLinkInterfaces.SWD)
    except Exception as exc:
        raise RuntimeError("Unable to select J-Link SWD target interface") from exc


def _connect_jlink(jlink: Any, device_candidates: tuple[str, ...]) -> None:
    unsupported: list[str] = []
    last_unsupported: Exception | None = None
    for device in device_candidates:
        try:
            jlink.connect(device)
            return
        except Exception as exc:
            if "Unsupported device selected" not in str(exc):
                raise RuntimeError(f"Unable to connect J-Link to target device {device}") from exc
            unsupported.append(device)
            last_unsupported = exc

    raise RuntimeError(
        "J-Link does not support any VCOM target device identifier: "
        + ", ".join(unsupported)
    ) from last_unsupported


def _jlink_device_candidates(vcom: dict[str, Any], firmware: dict[str, Any]) -> tuple[str, ...]:
    explicit = _clean_device_identifier(vcom.get("jlink_device"))
    if explicit:
        return (explicit,)

    candidates: list[str] = []
    for value in (vcom.get("device"), firmware.get("device")):
        device = _clean_device_identifier(value)
        if not device:
            continue
        _append_unique(candidates, _canonical_jlink_device(device))
        _append_unique(candidates, device)

    svd_device = _clean_device_identifier(vcom.get("svd_device"))
    if svd_device:
        _append_unique(candidates, svd_device)

    if not candidates:
        raise RuntimeError("vcom.device or firmware.device is required")
    return tuple(candidates)


def _clean_device_identifier(value: Any) -> str:
    return str(value or "").strip().upper()


def _canonical_jlink_device(device: str) -> str:
    return _DEVICE_PACKAGE_SUFFIX_RE.sub("", device)


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _resolve_jlink_library_path(config: dict[str, Any]) -> Path | None:
    tools = config.get("tools", {})
    tools = tools if isinstance(tools, dict) else {}

    explicit = tools.get("jlink_library") or os.getenv("CURRENT_MEASUREMENT_JLINK_LIBRARY")
    if explicit:
        path = Path(str(explicit))
        if path.is_file():
            return path
        raise RuntimeError(f"Configured J-Link library does not exist: {path}")

    commander = str(tools.get("commander") or "").strip()
    if not commander:
        return None

    commander_path = Path(commander)
    candidates = [
        directory / filename
        for directory in (commander_path.parent, commander_path.parent.parent / "jlink")
        for filename in _jlink_library_filenames()
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _jlink_library_filenames() -> tuple[str, ...]:
    if sys.platform.startswith("win"):
        return ("JLink_x64.dll",) if sys.maxsize > 2**32 else ("JLinkARM.dll",)
    if sys.platform.startswith("darwin"):
        return ("libjlinkarm.dylib",)
    return ("libjlinkarm.so", "libjlinkarm.so.8", "libjlinkarm.so.7")


def _configure_vcom_pin_mode(jlink: Any, gpio: dict[str, Any]) -> None:
    mode_register_address = _optional_u32(gpio.get("mode_register_address"))
    if mode_register_address is None:
        return
    mode_mask = _required_u32(gpio.get("mode_mask"), "vcom.gpio.mode_mask")
    mode_push_pull_value = _required_u32(
        gpio.get("mode_push_pull_value"),
        "vcom.gpio.mode_push_pull_value",
    )
    before = _read_u32(jlink, mode_register_address)
    after = (before & ~mode_mask) | mode_push_pull_value
    if after != before:
        _write_u32(jlink, mode_register_address, after)


def _read_u32(jlink: Any, address: int) -> int:
    values = jlink.memory_read32(address, 1)
    if not values:
        raise RuntimeError(f"J-Link read returned no data at 0x{address:08X}")
    return int(values[0])


def _write_u32(jlink: Any, address: int | None, value: int) -> None:
    if address is None:
        raise RuntimeError("J-Link write address is missing")
    jlink.memory_write32(address, [value & 0xFFFFFFFF])


def _required_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be an object")
    return value


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError(f"{name} is required")
    return text


def _required_u32(value: Any, name: str) -> int:
    if value in (None, ""):
        raise RuntimeError(f"{name} is required")
    return _parse_u32(value, name)


def _optional_u32(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return _parse_u32(value, "vcom.gpio register value")


def _parse_u32(value: Any, name: str) -> int:
    parsed = int(str(value).strip(), 0)
    if parsed < 0 or parsed > 0xFFFFFFFF:
        raise RuntimeError(f"{name} must fit in uint32")
    return parsed


def _parse_c_defines(header: str) -> dict[str, str]:
    defines: dict[str, str] = {}
    for line in header.splitlines():
        match = _DEFINE_RE.match(line)
        if match is None:
            continue
        defines[match.group("name")] = match.group("value")
    return defines


def _normalize_gpio_port(value: str) -> str:
    match = _GPIO_PORT_RE.match(value.strip())
    if match is None:
        raise ValueError(f"unsupported VCOM GPIO port value: {value}")
    return match.group("port").upper()


def _parse_gpio_pin(value: str) -> int:
    try:
        pin = int(value.strip(), 0)
    except ValueError as exc:
        raise ValueError(f"unsupported VCOM GPIO pin value: {value}") from exc
    if pin < 0:
        raise ValueError(f"VCOM GPIO pin must be non-negative: {pin}")
    return pin

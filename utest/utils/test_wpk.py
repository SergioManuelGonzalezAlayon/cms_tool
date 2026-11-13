import json
import subprocess
from pathlib import Path

import pytest

from utils.wpk import (
    VcomGpio,
    _jlink_device_candidates,
    _resolve_jlink_library_path,
    parse_board_vcom_gpio,
    parse_vcom_gpio_header,
    setDebugMode,
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


def test_set_debug_mode_bounds_commander_and_escalates_to_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 1234
        returncode = None

        def __init__(self) -> None:
            self.communicate_timeouts = []
            self.terminated = False
            self.killed = False
            self.reaped = False

        def communicate(self, timeout=None):
            self.communicate_timeouts.append(timeout)
            if len(self.communicate_timeouts) < 3:
                raise subprocess.TimeoutExpired("commander", timeout)
            self.returncode = -9
            self.reaped = True
            return "partial stdout", "partial stderr"

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    observed = {}
    process = FakeProcess()

    def fake_popen(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return process

    monkeypatch.setattr("utils.wpk.subprocess.Popen", fake_popen)

    with pytest.raises(RuntimeError, match="Commander timed out") as exc_info:
        setDebugMode(
            {
                "vcom": {"enabled": True},
                "dut": {"probe_sn": "440364944"},
                "tools": {"commander": "commander-cli"},
            },
            "MCU",
        )

    assert observed["command"] == [
        "commander-cli",
        "adapter",
        "dbgmode",
        "MCU",
        "--serialno",
        "440364944",
    ]
    assert "start_new_session" not in observed
    assert process.communicate_timeouts == [15.0, 5.0, 5.0]
    assert process.terminated is True
    assert process.killed is True
    assert process.reaped is True
    assert "partial stdout" in str(exc_info.value)
    assert "partial stderr" in str(exc_info.value)


def test_set_debug_mode_uses_a_bounded_commander_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            observed["timeout"] = timeout
            return "", ""

    def fake_popen(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr("utils.wpk.subprocess.Popen", fake_popen)

    setDebugMode(
        {
            "vcom": {"enabled": True},
            "dut": {"probe_sn": "440364944"},
            "tools": {"commander": "commander-cli"},
        },
        "OFF",
    )

    assert observed["timeout"] == 15.0
    assert observed["text"] is True
    assert observed["stdout"] is subprocess.PIPE
    assert observed["stderr"] is subprocess.PIPE


def test_set_debug_mode_reports_commander_that_survives_sigkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StuckProcess:
        pid = 4321
        returncode = None

        def __init__(self) -> None:
            self.communicate_timeouts = []
            self.terminated = False
            self.killed = False

        def communicate(self, timeout=None):
            self.communicate_timeouts.append(timeout)
            raise subprocess.TimeoutExpired("commander", timeout)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    process = StuckProcess()
    monkeypatch.setattr("utils.wpk.subprocess.Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(RuntimeError, match="did not exit after SIGKILL"):
        setDebugMode(
            {
                "vcom": {"enabled": True},
                "dut": {"probe_sn": "440364944"},
                "tools": {"commander": "commander-cli"},
            },
            "MCU",
        )

    assert process.communicate_timeouts == [15.0, 5.0, 5.0]
    assert process.terminated is True
    assert process.killed is True


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

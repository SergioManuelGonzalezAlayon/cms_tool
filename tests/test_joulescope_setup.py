import subprocess

import pytest

import joulescope_interface


def test_joulescope_usb_reset_is_noninteractive_and_bounded(monkeypatch) -> None:
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="reset", stderr="")

    monkeypatch.setattr(joulescope_interface.subprocess, "run", fake_run)

    joulescope_interface._reset_joulescope_usb("001/002")

    assert observed["command"] == ["usbreset", "001/002"]
    assert observed["timeout"] == joulescope_interface.USB_RESET_TIMEOUT_S
    assert observed["check"] is True


def test_joulescope_usb_reset_reports_timeout(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(joulescope_interface.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Joulescope USB reset timed out"):
        joulescope_interface._reset_joulescope_usb("001/002")


def test_capture_brackets_sample_with_vcom_and_debug_mode(monkeypatch) -> None:
    events: list[str] = []
    config = {"configured": True}
    monkeypatch.setattr(joulescope_interface, "_effective_wpk_config", lambda: config)
    monkeypatch.setattr(
        joulescope_interface,
        "setVCOM",
        lambda actual_config, enabled: events.append(
            "vcom_on" if enabled else "vcom_off"
        ),
    )
    monkeypatch.setattr(
        joulescope_interface,
        "setDebugMode",
        lambda actual_config, mode: events.append(f"debug_{mode}"),
    )
    js = _FakeCaptureJoulescope(events)

    assert js.capture(1.0) == {"current": "samples"}

    assert events == [
        "sleep",
        "vcom_off",
        "debug_OFF",
        "capture",
        "debug_MCU",
        "vcom_on",
        "wake",
    ]


def test_capture_restores_debug_mode_and_vcom_after_capture_failure(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(joulescope_interface, "_effective_wpk_config", lambda: {})
    monkeypatch.setattr(
        joulescope_interface,
        "setVCOM",
        lambda _config, enabled: events.append("vcom_on" if enabled else "vcom_off"),
    )
    monkeypatch.setattr(
        joulescope_interface,
        "setDebugMode",
        lambda _config, mode: events.append(f"debug_{mode}"),
    )
    js = _FakeCaptureJoulescope(events, fail_capture=True)

    with pytest.raises(RuntimeError, match="capture failed"):
        js.capture(1.0)

    assert events == [
        "sleep",
        "vcom_off",
        "debug_OFF",
        "capture",
        "debug_MCU",
        "vcom_on",
        "wake",
    ]


def test_capture_restores_debug_mode_and_vcom_after_keyboard_interrupt(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(joulescope_interface, "_effective_wpk_config", lambda: {})
    monkeypatch.setattr(
        joulescope_interface,
        "setVCOM",
        lambda _config, enabled: events.append("vcom_on" if enabled else "vcom_off"),
    )
    monkeypatch.setattr(
        joulescope_interface,
        "setDebugMode",
        lambda _config, mode: events.append(f"debug_{mode}"),
    )
    js = _FakeCaptureJoulescope(events)

    def interrupt_capture(*_args, **_kwargs):
        events.append("capture")
        raise KeyboardInterrupt

    monkeypatch.setattr(js, "read", interrupt_capture)

    with pytest.raises(KeyboardInterrupt):
        js.capture(1.0)

    assert events == [
        "sleep",
        "vcom_off",
        "debug_OFF",
        "capture",
        "debug_MCU",
        "vcom_on",
        "wake",
    ]


class _FakeCaptureJoulescope(joulescope_interface.WakePinJoulescope):
    def __init__(
        self,
        events: list[str],
        *,
        fail_capture: bool = False,
    ) -> None:
        self.events = events
        self.fail_capture = fail_capture
        self.use_wake_pin = True
        self.dut_sleep_settle_ms = 0
        self.dut_wake_gpio = 0
        self.dut_awake_settle_s = 0
        self.capture_sequence = 0
        self.driver = object()
        self.signals_full = ["current"]
        self.capture_data = None

    def _require_driver(self) -> None:
        return

    def sleepDUT(self) -> None:
        self.events.append("sleep")

    def wakeDUT(self) -> None:
        self.events.append("wake")

    def read(self, *_args, **_kwargs):
        self.events.append("capture")
        if self.fail_capture:
            raise RuntimeError("capture failed")
        return {"current": "samples"}

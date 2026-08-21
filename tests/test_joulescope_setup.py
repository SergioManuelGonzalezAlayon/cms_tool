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

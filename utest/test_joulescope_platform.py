import subprocess
import sys
from unittest.mock import Mock

import pytest

import cms_bench
import joulescope_interface


class _FakeDriver:
    def __init__(self, device_paths):
        self._device_paths = device_paths
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def device_paths(self):
        return self._device_paths


@pytest.mark.parametrize(
    ("device_path", "expected_model"),
    [
        ("u/js220/005757", "js220"),
        ("u/js320/8W2A", "js320"),
        ("u/&js220/005757", "js220"),
    ],
)
def test_read_js_model_uses_driver_device_paths(monkeypatch, device_path, expected_model):
    driver = _FakeDriver([device_path])
    monkeypatch.setattr(cms_bench, "Driver", lambda: driver)

    assert cms_bench._read_js_model() == expected_model
    assert driver.closed


def test_read_js_model_rejects_unsupported_device(monkeypatch):
    monkeypatch.setattr(cms_bench, "Driver", lambda: _FakeDriver(["u/js110/1234"]))

    with pytest.raises(ValueError, match="unsupported Joulescope model detected: js110"):
        cms_bench._read_js_model()


def test_read_js_model_reports_no_connected_joulescope(monkeypatch):
    monkeypatch.setattr(cms_bench, "Driver", lambda: _FakeDriver([]))

    with pytest.raises(ValueError, match="not found in connected driver devices"):
        cms_bench._read_js_model()


def test_windows_skips_linux_usbreset(monkeypatch):
    monkeypatch.setattr(joulescope_interface, "_platform_system", lambda: "Windows")
    run = Mock(side_effect=AssertionError("usbreset must not run on Windows"))
    monkeypatch.setattr(joulescope_interface.subprocess, "run", run)

    assert joulescope_interface._reset_joulescope_usb("005757") is None
    run.assert_not_called()


@pytest.mark.skipif(sys.platform == "win32", reason="usbreset behavior is Linux-only")
def test_linux_keeps_usbreset_behavior(monkeypatch):
    monkeypatch.setattr(joulescope_interface, "_platform_system", lambda: "Linux")
    run = Mock(
        return_value=subprocess.CompletedProcess(
            ["usbreset", "16d0:10ba"], 0, stdout="", stderr=""
        )
    )
    monkeypatch.setattr(joulescope_interface.subprocess, "run", run)

    assert joulescope_interface._reset_joulescope_usb("16d0:10ba") is None
    run.assert_called_once_with(
        ["usbreset", "16d0:10ba"],
        capture_output=True,
        text=True,
        check=True,
        timeout=joulescope_interface.USB_RESET_TIMEOUT_S,
    )

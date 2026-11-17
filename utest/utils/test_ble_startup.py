from types import SimpleNamespace

import pytest

import ble
from utils.bgapi_utils import (
    BGAPIEventTimeoutError,
    RETRYABLE_STARTUP_EXIT_CODE,
    RetryableDeviceStartupError,
)


class _PytestExitCalled(Exception):
    pass


def _device(calls=None) -> SimpleNamespace:
    calls = calls if calls is not None else []

    def record(name):
        return lambda: calls.append(name)

    return SimpleNamespace(
        open=record("open"),
        close=record("close"),
        bt=SimpleNamespace(
            system=SimpleNamespace(
                hello=lambda: calls.append("hello") or SimpleNamespace(result=0),
                reboot=record("reboot"),
            )
        ),
    )


def _event_timeout(event_name: str) -> BGAPIEventTimeoutError:
    return BGAPIEventTimeoutError(
        event_class="system",
        event_name=event_name,
        max_time=10,
        event_timeout=5,
    )


def test_open_ble_dev_raises_retryable_error_for_missing_boot_event(monkeypatch):
    calls = []

    def fake_check_event(*args, **kwargs):
        calls.append((args[1], args[2]))
        if args[2] == "boot":
            raise _event_timeout("boot")

    monkeypatch.setattr(ble, "bgapi_check_event", fake_check_event)

    with pytest.raises(RetryableDeviceStartupError, match="retrying this pytest job is allowed"):
        ble.open_ble_dev(
            _device(),
            wakeUp=SimpleNamespace(setDUTAwake=lambda: None),
            open_retries=1,
            retry_sleep_s=0,
        )

    assert calls[-1] == ("system", "boot")


def test_open_ble_dev_wakes_before_open_and_accepts_missing_awake_event(monkeypatch):
    calls = []

    def fake_check_event(dev, event_class, event_name, condition, **kwargs):
        calls.append(("wait", event_class, event_name))
        if event_name == "awake":
            raise _event_timeout("awake")

    monkeypatch.setattr(ble, "bgapi_check_event", fake_check_event)
    wake_up = SimpleNamespace(setDUTAwake=lambda: calls.append("wake"))

    result = ble.open_ble_dev(
        _device(calls),
        wakeUp=wake_up,
        open_retries=1,
        retry_sleep_s=0,
    )

    assert result is not None
    assert calls == [
        "wake",
        "open",
        ("wait", "system", "awake"),
        "hello",
        "reboot",
        ("wait", "system", "boot"),
        "wake",
        "hello",
    ]


def test_open_ble_dev_retries_with_cleanup_and_probes_after_boot(monkeypatch):
    calls = []
    boot_attempts = 0

    def fake_check_event(dev, event_class, event_name, condition, **kwargs):
        nonlocal boot_attempts
        calls.append(("wait", event_class, event_name))
        if event_name == "boot":
            boot_attempts += 1
            if boot_attempts == 1:
                raise _event_timeout("boot")

    monkeypatch.setattr(ble, "bgapi_check_event", fake_check_event)

    result = ble.open_ble_dev(
        _device(calls),
        wakeUp=SimpleNamespace(setDUTAwake=lambda: calls.append("wake")),
        open_retries=2,
        retry_sleep_s=0,
    )

    assert result is not None
    assert calls == [
        "wake",
        "open",
        ("wait", "system", "awake"),
        "hello",
        "reboot",
        ("wait", "system", "boot"),
        "close",
        "wake",
        "open",
        ("wait", "system", "awake"),
        "hello",
        "reboot",
        ("wait", "system", "boot"),
        "wake",
        "hello",
    ]


def test_open_ble_dev_reboots_tester_and_probes_after_boot(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ble,
        "bgapi_check_event",
        lambda *args, **kwargs: calls.append(("wait", args[1], args[2])),
    )

    ble.open_ble_dev(_device(calls), open_retries=1, retry_sleep_s=0)

    assert calls == [
        "open",
        "hello",
        "reboot",
        ("wait", "system", "boot"),
        "hello",
    ]


def test_pytest_boundary_maps_retryable_startup_error_to_dedicated_exit_code(monkeypatch):
    startup_error = RetryableDeviceStartupError(_event_timeout("boot"), attempts=1)

    def fake_exit(message, returncode):
        assert message == str(startup_error)
        assert returncode == RETRYABLE_STARTUP_EXIT_CODE
        raise _PytestExitCalled

    monkeypatch.setattr(
        ble,
        "open_ble_dev",
        lambda *args, **kwargs: (_ for _ in ()).throw(startup_error),
    )
    monkeypatch.setattr(ble.pytest, "exit", fake_exit)

    with pytest.raises(_PytestExitCalled):
        ble.open_ble_dev_for_pytest(_device())


def test_open_ble_dev_does_not_emit_retryable_code_for_other_timeouts(monkeypatch):
    other_timeout = BGAPIEventTimeoutError(
        event_class="connection",
        event_name="opened",
        max_time=10,
        event_timeout=5,
    )
    monkeypatch.setattr(ble, "bgapi_check_event", lambda *args, **kwargs: (_ for _ in ()).throw(other_timeout))
    monkeypatch.setattr(
        ble.pytest,
        "exit",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected pytest exit")),
    )

    with pytest.raises(RuntimeError, match="Failed to open/reset BLE device"):
        ble.open_ble_dev(
            _device(),
            wakeUp=SimpleNamespace(setDUTAwake=lambda: None),
            open_retries=1,
            retry_sleep_s=0,
        )

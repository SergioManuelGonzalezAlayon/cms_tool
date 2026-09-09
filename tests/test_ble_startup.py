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


def _device() -> SimpleNamespace:
    return SimpleNamespace(
        open=lambda: None,
        close=lambda: None,
        bt=SimpleNamespace(system=SimpleNamespace(reboot=lambda: None)),
    )


def _event_timeout(event_name: str) -> BGAPIEventTimeoutError:
    return BGAPIEventTimeoutError(
        event_class="system",
        event_name=event_name,
        max_time=10,
        event_timeout=5,
    )


@pytest.mark.parametrize("event_name", ["boot", "awake"])
def test_open_ble_dev_raises_retryable_error_for_missing_startup_event(event_name, monkeypatch):
    calls = []

    def fake_check_event(*args, **kwargs):
        calls.append((args[1], args[2]))
        if args[2] == event_name:
            raise _event_timeout(event_name)

    monkeypatch.setattr(ble, "bgapi_check_event", fake_check_event)
    wake_up = SimpleNamespace(setDUTAwake=lambda: None) if event_name == "awake" else None

    with pytest.raises(RetryableDeviceStartupError, match="retrying this pytest job is allowed"):
        ble.open_ble_dev(_device(), wakeUp=wake_up, open_retries=1, retry_sleep_s=0)

    assert calls[-1] == ("system", event_name)


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
        ble.open_ble_dev(_device(), open_retries=1, retry_sleep_s=0)

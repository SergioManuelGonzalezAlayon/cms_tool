from datetime import timezone

from cms_protocol import (
    FirmwareData,
    FirmwareImage,
    MeasurementMode,
    MeasurementRequest,
    MeasurementState,
    SuiteSelector,
    VariableFilter,
)
from cmsd import CMSDaemon


class _DormantThread:
    """Capture background submission without executing bench work."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def start(self):
        return None


def _staged_request() -> MeasurementRequest:
    return MeasurementRequest(
        request_id="firmware-staging-request",
        selector=SuiteSelector(mode="ble", feature="", config="default"),
        firmware=FirmwareData(
            dut_img=FirmwareImage(name="dut.s37", url="dut.s37"),
            tester_img=FirmwareImage(name="tester.s37", url="tester.s37"),
        ),
    )


def test_staged_executions_get_unique_daemon_identities(monkeypatch):
    monkeypatch.setattr("cmsd.threading.Thread", _DormantThread)
    daemon = CMSDaemon()
    daemon.last_firmware_request = _staged_request()

    first = daemon.start_staged_test(
        mode=MeasurementMode.DEFAULT.value,
        feature="beacon",
        run_name="Nightly Beacon Run",
    )
    second = daemon.start_staged_test(
        mode=MeasurementMode.DEFAULT.value,
        feature="beacon",
        run_name="Nightly Beacon Run",
    )

    assert first.identity is not None
    assert second.identity is not None
    assert first.identity.run_name == "Nightly Beacon Run"
    assert first.measurement_id == first.identity.measurement_id
    assert second.measurement_id == second.identity.measurement_id
    assert first.measurement_id != second.measurement_id
    assert first.measurement_id.startswith("nightly-beacon-run-")
    assert first.identity.created_at.tzinfo is timezone.utc


def test_staged_execution_derives_readable_name_without_client_override(monkeypatch):
    monkeypatch.setattr("cmsd.threading.Thread", _DormantThread)
    daemon = CMSDaemon()
    daemon.last_firmware_request = _staged_request()

    result = daemon.start_staged_test(
        mode=MeasurementMode.DEFAULT.value,
        feature="beacon",
        config="default",
    )

    assert result.identity is not None
    assert result.identity.run_name == "beacon-default-default"
    assert "firmware-staging-request" not in result.measurement_id


def test_staged_execution_preserves_requested_config_hash(monkeypatch):
    captured = {}

    class CapturingThread(_DormantThread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr("cmsd.threading.Thread", CapturingThread)
    daemon = CMSDaemon()
    daemon.last_firmware_request = _staged_request()

    daemon.start_staged_test(
        mode=MeasurementMode.DEFAULT.value,
        feature="beacon",
        config="dcdc",
        config_hash="sha256:dcdc",
    )

    request = captured["kwargs"]["args"][0]
    assert request.selector.config == "dcdc"
    assert request.selector.config_hash == "sha256:dcdc"


def test_staged_execution_preserves_variable_filters(monkeypatch):
    captured = {}

    class CapturingThread(_DormantThread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["kwargs"] = kwargs

    monkeypatch.setattr("cmsd.threading.Thread", CapturingThread)
    daemon = CMSDaemon()
    daemon.last_firmware_request = _staged_request()
    filters = (VariableFilter("phy", "2m"),)

    result = daemon.start_staged_test(
        mode=MeasurementMode.FULL_SWEEP.value,
        feature="beacon",
        variable_filters=filters,
    )

    request = captured["kwargs"]["args"][0]
    assert result.state is MeasurementState.ACCEPTED
    assert request.selector.variable_filters == filters

from cms_protocol import (
    BenchCheckResult,
    BenchCheckStatus,
    BenchHealthReport,
    BenchState,
    FirmwareData,
    FirmwareImage,
    MeasurementMode,
    MeasurementRequest,
    MeasurementState,
    SuiteSelector,
)
from cmsd import CMSDaemon
from types import SimpleNamespace


class _DormantThread:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def start(self):
        return None


class _JoulescopeReadinessBench:
    def __init__(self, check):
        self.check = check
        self.calls = 0
        self.resources = SimpleNamespace(joulescope_model="js320")

    def check_joulescope_communication(self):
        self.calls += 1
        return self.check


def _staged_request() -> MeasurementRequest:
    return MeasurementRequest(
        request_id="staged-request",
        selector=SuiteSelector(mode="ble", feature="", config="default"),
        firmware=FirmwareData(
            dut_img=FirmwareImage(name="dut.s37", url="dut.s37"),
            tester_img=FirmwareImage(name="tester.s37", url="tester.s37"),
        ),
    )


def test_readiness_failure_names_the_failed_hardware_check(monkeypatch):
    daemon = CMSDaemon()
    daemon.initialized = True
    joulescope_check = BenchCheckResult(
        name="joulescope",
        status=BenchCheckStatus.FAIL,
        summary="Joulescope communication check failed",
        detail="no response from JS320",
    )
    daemon.bench = _JoulescopeReadinessBench(joulescope_check)
    report = BenchHealthReport(
        state=BenchState.ERROR,
        summary="daemon health failed: joulescope",
        checks=[joulescope_check],
    )
    monkeypatch.setattr(daemon, "get_health", lambda: report)

    failure = daemon._measurement_readiness_failure()

    assert "JS320 is not ready" in failure
    assert "Check the CMS Bench configuration" in failure
    assert "no response from JS320" in failure


def test_readiness_rejects_unverified_optional_joulescope(monkeypatch):
    daemon = CMSDaemon()
    daemon.initialized = True
    joulescope_check = BenchCheckResult(
        name="joulescope",
        status=BenchCheckStatus.WARN,
        summary="Joulescope mapping is not configured",
    )
    daemon.bench = _JoulescopeReadinessBench(joulescope_check)
    report = BenchHealthReport(
        state=BenchState.IDLE,
        summary="bench is ready with warnings",
        checks=[joulescope_check],
    )
    monkeypatch.setattr(daemon, "get_health", lambda: report)

    failure = daemon._measurement_readiness_failure()

    assert "JS320 is not ready" in failure
    assert "Joulescope mapping is not configured" in failure


def test_readiness_runs_joulescope_probe_before_accepting(monkeypatch):
    daemon = CMSDaemon()
    daemon.initialized = True
    daemon.bench = _JoulescopeReadinessBench(
        BenchCheckResult(
            name="joulescope",
            status=BenchCheckStatus.PASS,
            summary="JS320 communication verified",
        )
    )
    monkeypatch.setattr(
        daemon,
        "get_health",
        lambda: BenchHealthReport(
            state=BenchState.IDLE,
            summary="bench is idle",
            checks=[daemon.bench.check],
        ),
    )

    failure = daemon._measurement_readiness_failure()

    assert failure == ""
    assert daemon.bench.calls == 1


def test_staged_measurement_is_rejected_before_accept_when_hardware_is_not_ready(
    monkeypatch,
):
    daemon = CMSDaemon()
    daemon.initialized = True
    daemon.last_firmware_request = _staged_request()
    monkeypatch.setattr(
        daemon,
        "_measurement_readiness_failure",
        lambda: "joulescope: no response from JS320",
    )

    def unexpected_thread(*_args, **_kwargs):
        raise AssertionError("measurement thread must not be started")

    monkeypatch.setattr("cmsd.threading.Thread", unexpected_thread)

    result = daemon.start_staged_test(
        mode=MeasurementMode.DEFAULT.value,
        feature="beacon",
    )

    assert result.state is MeasurementState.FAILED
    assert result.measurement_id
    assert "bench readiness check failed" in result.message
    assert daemon.active_measurement_id is None


def test_staged_measurement_is_accepted_after_successful_readiness_check(monkeypatch):
    daemon = CMSDaemon()
    daemon.initialized = True
    daemon.last_firmware_request = _staged_request()
    monkeypatch.setattr(daemon, "_measurement_readiness_failure", lambda: "")
    monkeypatch.setattr("cmsd.threading.Thread", _DormantThread)

    result = daemon.start_staged_test(
        mode=MeasurementMode.DEFAULT.value,
        feature="beacon",
    )

    assert result.state is MeasurementState.ACCEPTED
    assert daemon.active_measurement_id == result.measurement_id


def test_synchronous_measurement_fails_before_accept_when_hardware_is_not_ready(
    monkeypatch,
):
    daemon = CMSDaemon()
    daemon.initialized = True
    monkeypatch.setattr(
        daemon,
        "_measurement_readiness_failure",
        lambda: "joulescope: no response from JS320",
    )

    result = daemon.execute_measurement(_staged_request())

    assert result.state is MeasurementState.FAILED
    assert "bench readiness check failed" in result.message
    assert daemon.active_measurement_id is None

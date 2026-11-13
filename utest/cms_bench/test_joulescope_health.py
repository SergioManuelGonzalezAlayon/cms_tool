import subprocess

import cms_bench
from cms_protocol import BenchCheckStatus


class _JoulescopeDriver:
    def __init__(self, *, query_error=None):
        self.query_error = query_error
        self.calls = []

    def device_paths(self, *, timeout):
        self.calls.append(("device_paths", timeout))
        return ["u/js320/test-serial"]

    def open(self, path, *, mode, timeout):
        self.calls.append(("open", path, mode, timeout))

    def query(self, topic, *, timeout):
        self.calls.append(("query", topic, timeout))
        if self.query_error is not None:
            raise self.query_error
        return 0x01020304

    def close(self, path, *, timeout):
        self.calls.append(("close", path, timeout))

    def finalize(self, *, timeout):
        self.calls.append(("finalize", timeout))


def _resources():
    return cms_bench.BenchResources(
        joulescope_serial="test-serial",
        joulescope_model="js320",
    )


def test_joulescope_health_opens_configured_js320_and_queries_firmware(monkeypatch):
    driver = _JoulescopeDriver()
    monkeypatch.setattr(cms_bench, "Driver", lambda: driver)

    check = cms_bench._joulescope_communication_check(_resources())

    assert check.status is BenchCheckStatus.PASS
    assert check.summary == "JS320 communication verified"
    assert "firmware_version=16909060" in check.detail
    assert driver.calls[0][0] == "device_paths"
    assert driver.calls[1][0:3] == ("open", "u/js320/test-serial", "restore")
    assert driver.calls[2][0:2] == (
        "query",
        "u/js320/test-serial/c/fw/version",
    )
    assert driver.calls[3][0:2] == ("close", "u/js320/test-serial")
    assert driver.calls[4][0] == "finalize"
    assert all(0 < call[-1] <= cms_bench.JOULESCOPE_PROBE_TIMEOUT_S for call in driver.calls[:3])
    assert all(
        0 < call[-1] <= cms_bench.JOULESCOPE_PROBE_CLEANUP_TIMEOUT_S
        for call in driver.calls[3:]
    )


def test_joulescope_health_reports_query_failure_and_releases_driver(monkeypatch):
    driver = _JoulescopeDriver(query_error=TimeoutError("no response from JS320"))
    monkeypatch.setattr(cms_bench, "Driver", lambda: driver)

    check = cms_bench._joulescope_communication_check(_resources())

    assert check.status is BenchCheckStatus.FAIL
    assert "no response from JS320" in check.detail
    assert ("close", "u/js320/test-serial", cms_bench.JOULESCOPE_PROBE_CLEANUP_TIMEOUT_S) in driver.calls
    assert ("finalize", cms_bench.JOULESCOPE_PROBE_CLEANUP_TIMEOUT_S) in driver.calls


def test_joulescope_health_fails_if_configured_serial_is_not_connected(monkeypatch):
    driver = _JoulescopeDriver()
    monkeypatch.setattr(cms_bench, "Driver", lambda: driver)
    resources = cms_bench.BenchResources(
        joulescope_serial="different-serial",
        joulescope_model="js320",
    )

    check = cms_bench._joulescope_communication_check(resources)

    assert check.status is BenchCheckStatus.FAIL
    assert "matched 0 connected devices" in check.detail
    assert not any(call[0] == "open" for call in driver.calls)
    assert driver.calls[-1][0] == "finalize"


def test_joulescope_health_checks_usb_id_with_lsusb_without_driver_path_inference(monkeypatch):
    def unexpected_driver():
        raise AssertionError("USB ID mapping must not infer a device from driver paths")

    monkeypatch.setattr(cms_bench, "Driver", unexpected_driver)
    lsusb_calls = []

    def lsusb(command, **kwargs):
        lsusb_calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="Bus 003 Device 031: ID 16d0:135a MCS Joulescope JS320\n",
            stderr="",
        )

    monkeypatch.setattr(cms_bench.subprocess, "run", lsusb)
    resources = cms_bench.BenchResources(
        joulescope_id="16d0:135a",
        joulescope_model="js320",
    )

    check = cms_bench._joulescope_communication_check(resources)

    assert check.status is BenchCheckStatus.PASS
    assert check.summary == "JS320 USB device detected by lsusb"
    assert lsusb_calls[0][0] == ["lsusb", "-d", "16d0:135a"]
    assert 0 < lsusb_calls[0][1]["timeout"] <= cms_bench.JOULESCOPE_PROBE_TIMEOUT_S


def test_joulescope_health_fails_if_usb_id_is_missing_from_lsusb(monkeypatch):
    def unexpected_driver():
        raise AssertionError("missing USB ID must fail before a driver probe")

    monkeypatch.setattr(cms_bench, "Driver", unexpected_driver)
    monkeypatch.setattr(
        cms_bench.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="",
        ),
    )
    resources = cms_bench.BenchResources(
        joulescope_id="16d0:135a",
        joulescope_model="js320",
    )

    check = cms_bench._joulescope_communication_check(resources)

    assert check.status is BenchCheckStatus.FAIL
    assert "is not listed by lsusb" in check.detail


def test_health_check_defers_joulescope_probe_during_active_measurement(monkeypatch):
    bench = cms_bench.CMSBench(
        identity=cms_bench.BenchIdentity(bench_id="test-bench"),
        resources=_resources(),
    )
    bench.state = cms_bench.BenchState.TESTING

    def unexpected_probe(_resources):
        raise AssertionError("health polling must not open an active measurement device")

    monkeypatch.setattr(cms_bench, "_joulescope_communication_check", unexpected_probe)

    report = bench.health_check()

    joulescope_check = next(check for check in report.checks if check.name == "joulescope")
    assert joulescope_check.status is BenchCheckStatus.WARN
    assert "deferred while measurement is active" in joulescope_check.summary


def test_health_polling_does_not_probe_joulescope(monkeypatch):
    bench = cms_bench.CMSBench(
        identity=cms_bench.BenchIdentity(bench_id="test-bench"),
        resources=_resources(),
    )

    def unexpected_probe(_resources):
        raise AssertionError("health polling must not open the Joulescope")

    monkeypatch.setattr(cms_bench, "_joulescope_communication_check", unexpected_probe)

    report = bench.health_check()

    joulescope_check = next(check for check in report.checks if check.name == "joulescope")
    assert joulescope_check.status is BenchCheckStatus.WARN
    assert "has not yet been checked" in joulescope_check.summary

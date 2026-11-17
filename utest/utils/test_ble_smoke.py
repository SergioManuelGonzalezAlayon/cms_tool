from types import SimpleNamespace

import pytest

import ble


def _response(result=0):
    return SimpleNamespace(result=result)


def _smoke_devices(calls, *, scanner_start=None):
    def record(role, operation, response=None):
        def call(*args):
            calls.append((role, operation, *args))
            if isinstance(response, Exception):
                raise response
            return response if response is not None else _response()

        return call

    dut = SimpleNamespace(
        bt=SimpleNamespace(
            system=SimpleNamespace(hello=record("DUT", "hello")),
            extended_advertiser=SimpleNamespace(
                CONNECTION_MODE_NON_CONNECTABLE=0,
                start=record("DUT", "advertiser_start"),
            ),
            advertiser=SimpleNamespace(
                stop=record("DUT", "advertiser_stop"),
                delete_set=record("DUT", "advertiser_delete"),
            ),
        )
    )
    tester = SimpleNamespace(
        bt=SimpleNamespace(
            system=SimpleNamespace(hello=record("Tester", "hello")),
            scanner=SimpleNamespace(
                SCAN_MODE_SCAN_MODE_PASSIVE=0,
                DISCOVER_MODE_DISCOVER_OBSERVATION=1,
                start=record("Tester", "scanner_start", scanner_start),
                stop=record("Tester", "scanner_stop"),
            ),
        )
    )

    def configure_scanner(*, mode):
        calls.append(("Tester", "scanner_set_parameters", mode))
        return SimpleNamespace(phy=1)

    return dut, tester, configure_scanner


def test_firmware_smoke_checks_both_ncp_paths_and_cleans_up():
    calls = []
    dut, tester, configure_scanner = _smoke_devices(calls)

    ble.test_firmware_smoke(
        Config={},
        DUT=dut,
        Tester=tester,
        DUTAdvConfig=SimpleNamespace(set=3),
        TesterScanConfig=configure_scanner,
    )

    assert calls == [
        ("DUT", "hello"),
        ("Tester", "hello"),
        ("DUT", "advertiser_start", 3, 0, 0),
        ("Tester", "scanner_set_parameters", 0),
        ("Tester", "scanner_start", 1, 1),
        ("Tester", "scanner_stop"),
        ("DUT", "advertiser_stop", 3),
        ("DUT", "advertiser_delete", 3),
    ]


def test_firmware_smoke_cleans_up_advertiser_when_scanner_start_fails():
    calls = []
    dut, tester, configure_scanner = _smoke_devices(
        calls,
        scanner_start=RuntimeError("No response"),
    )

    with pytest.raises(RuntimeError, match="No response"):
        ble.test_firmware_smoke(
            Config={},
            DUT=dut,
            Tester=tester,
            DUTAdvConfig=SimpleNamespace(set=3),
            TesterScanConfig=configure_scanner,
        )

    assert calls[-3:] == [
        ("Tester", "scanner_stop"),
        ("DUT", "advertiser_stop", 3),
        ("DUT", "advertiser_delete", 3),
    ]

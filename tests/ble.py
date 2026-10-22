import pytest
import logging
import json
import os
from dataclasses import dataclass, asdict
from time import sleep
from bgapi.bglib import BGLib
from bgapi.serialconnector import SerialConnector
from joulescope_interface import JoulescopeDev, JoulescopeUsbReset
from utils.bgapi_utils import *
from conftest import *

# TODO - BGAPI device interfaces in pytest so that we can use the HWMux thingy
MAX_TEST_RETRIES = 5
MAX_TEST_RETRY_WAIT = 1.0
DUT_OPEN_RETRIES = 5
DUT_OPEN_RETRY_SLEEP_S = 1.0
DEVICE_OPEN_RETRIES = 5
log = logging.getLogger(__name__)


def _log_fixture_cleanup_failure(fixture_name: str, exc: Exception) -> None:
    # Joulescope capture can leave BGAPI unavailable during teardown after the
    # result data has already been saved.
    log.warning("%s cleanup skipped; result remains passed with warning: %s", fixture_name, exc)


def _format_bd_addr(address) -> str:
    return str(address)


def _discovery_scan_phy_for_advertiser(phy: str | int) -> int:
    return discovery_scan_phy_for_advertiser(phy)


def _set_advertiser_phy(device, advertising_set: int, primary_phy: int, secondary_phy: int):
    """Set advertising PHY across BGAPI versions that moved this command.

    SiSDK 2025.12.3's ``bluetooth_le_host/api/sl_bt.xapi`` exposes it as
    ``extended_advertiser.set_phy``; older xapi files may expose it on
    ``advertiser`` instead.
    """

    extended_advertiser = getattr(device.bt, "extended_advertiser", None)
    if extended_advertiser is not None and hasattr(extended_advertiser, "set_phy"):
        return extended_advertiser.set_phy(advertising_set, primary_phy, secondary_phy)
    return device.bt.advertiser.set_phy(advertising_set, primary_phy, secondary_phy)


def open_ble_dev(dev,
                 wakeUp=None,
                 *,
                 open_retries=DUT_OPEN_RETRIES,
                 retry_sleep_s=0.5,
                 boot_event_timeout=5,
                 boot_max_time=10,
                 awake_event_timeout=5,
                 awake_max_time=10):
    last_error = None

    for attempt in range(1, open_retries + 1):
        try:
            dev.open()
            dev.bt.system.reboot()
            bgapi_check_event(
                dev,
                "system",
                "boot",
                lambda evt: True,
                eventTimeout=boot_event_timeout,
                maxTime=boot_max_time,
            )

            if wakeUp is not None:
                wakeUp.setDUTAwake()
                bgapi_check_event(
                    dev,
                    "system",
                    "awake",
                    lambda evt: True,
                    eventTimeout=awake_event_timeout,
                    maxTime=awake_max_time,
                )

            if attempt > 1:
                log.info("BLE open/reset recovered on attempt %d/%d", attempt, open_retries)
            log.info("BLE device reported system.boot after reset")
            return dev
        except Exception as exc:
            last_error = exc
            log.warning(
                "BLE open/reset attempt %d/%d failed: %s",
                attempt,
                open_retries,
                exc,
            )
            try:
                dev.close()
            except Exception:
                pass
            if attempt < open_retries and retry_sleep_s > 0:
                sleep(retry_sleep_s)

    if is_retryable_startup_event_timeout(last_error):
        raise RetryableDeviceStartupError(last_error, open_retries) from last_error

    raise RuntimeError(
        f"Failed to open/reset BLE device after {open_retries} attempt(s)"
    ) from last_error


def open_ble_dev_for_pytest(*args, **kwargs):
    """Map retryable device startup failures to the orchestrator's pytest code."""

    try:
        return open_ble_dev(*args, **kwargs)
    except RetryableDeviceStartupError as exc:
        pytest.exit(str(exc), returncode=RETRYABLE_STARTUP_EXIT_CODE)


def _match_pawr_advertiser_report(evt, dut_address, *, require_periodic_interval: bool) -> bool:
    if evt.address == dut_address:
        log.info(
            "Matched PAwR advertisement: address=%s address_type=%s adv_sid=%s periodic_interval=%s",
            _format_bd_addr(evt.address),
            evt.address_type,
            evt.adv_sid,
            getattr(evt, "periodic_interval", 0),
        )
    if evt.address != dut_address:
        return False
    if require_periodic_interval:
        return getattr(evt, "periodic_interval", 0) != 0
    return True


def _log_sync_event(evt) -> bool:
    if evt._class_name in {"periodic_sync", "pawr_sync", "sync"}:
        details = []
        for field_name in (
            "sync",
            "adv_sid",
            "address",
            "address_type",
            "adv_interval",
            "num_subevents",
            "event_counter",
            "subevent",
        ):
            if hasattr(evt, field_name):
                value = getattr(evt, field_name)
                if field_name == "address":
                    value = _format_bd_addr(value)
                details.append(f"{field_name}={value}")
        suffix = f" ({', '.join(details)})" if details else ""
        log.info("Sync event: %s.%s%s", evt._class_name, evt._msg_name, suffix)
    return False

@pytest.fixture
def BLEDev(EffectiveDevConfig):
    created =[]

    def _create_ble_dev(device : str):
        xapi_path = EffectiveDevConfig.get("ble", {}).get("xapi")
        if xapi_path:
            os.environ["BGAPI_XAPI"] = str(xapi_path)

        port = EffectiveDevConfig.get(device, {}).get("serial_port")

        if port is None:
            raise RuntimeError("No serial_port found")

        dev = get_ble_dev(port)
        
        created.append((device, port, dev))

        return dev

    yield _create_ble_dev
    
    for dev_name, dev_port, dev in reversed(created):
        try:
            dev.close()
        except Exception as e:
            log.exception(f"Failed to close BGAPI connection on:\n{e}")
            log.exception(f"Device {dev_name} on port {dev_port}.\n")


@pytest.fixture
def DUT(BLEDev,
        JoulescopeDev,
        ProvisionFirmware):
    dut = BLEDev("dut")
    dut = open_ble_dev_for_pytest(
        dut,
        wakeUp=JoulescopeDev,
        open_retries=DUT_OPEN_RETRIES,
        retry_sleep_s=DUT_OPEN_RETRY_SLEEP_S,
    )
    if ProvisionFirmware is not None:
        ProvisionFirmware("dut")
    yield dut

@pytest.fixture
def Tester(BLEDev,
           ProvisionFirmware):
    tester = BLEDev("tester")
    tester = open_ble_dev_for_pytest(tester)
    if ProvisionFirmware is not None:
        ProvisionFirmware("tester")
    yield tester

@pytest.fixture
def DUTLegAdvConfig(DUT,
                 Config) -> AdvertiserConfig:
    log.info("Configuring DUT Advertiser")

    config = dataclass_from_dict(AdvertiserConfig, Config.get("advertiser",{}))
    config = normalize_advertiser_config(config)
    rsp = DUT.bt.advertiser.create_set()
    config.set = rsp.handle

    DUT.bt.advertiser.set_timing(config.set,
                                 config.min_interval,
                                 config.max_interval,
                                 config.duration,
                                 config.max_events)
    log.info("DUT Advertiser configured")

    DUT.bt.advertiser.set_tx_power(config.set,config.tx_power)
    _set_advertiser_phy(DUT, config.set, config.primary_phy, config.primary_phy)
    log.info(
        "DUT legacy advertiser PHY configured: primary=%s",
        advertising_phy_to_str(config.primary_phy),
    )
    DUT.bt.legacy_advertiser.set_data(config.set,
                                      0, # Advertising data packet
                                      config.payload)

    return config

@pytest.fixture
def DUTLegAdvStart(DUT,
                   DUTLegAdvConfig):
    started = False

    def start(*, connectable_mode, flags=0):
        nonlocal started

        DUT.bt.legacy_advertiser.start(DUTLegAdvConfig.set, connectable_mode)
        log.info("DUT Advertising started")
        started = True

    yield start

    try:
        if started:
            DUT.bt.advertiser.stop(DUTLegAdvConfig.set)
            log.info("DUT Advertising stopped")
        DUT.bt.advertiser.delete_set(DUTLegAdvConfig.set)
        log.info("DUT Advertising set deleted")
    except Exception as exc:
        _log_fixture_cleanup_failure("DUT legacy advertiser", exc)

@pytest.fixture
def DUTAdvConfig(DUT,
                 Config) -> AdvertiserConfig:
    log.info("Configuring DUT Advertiser")

    config = dataclass_from_dict(AdvertiserConfig, Config.get("advertiser",{}))
    config = normalize_advertiser_config(config)
    rsp = DUT.bt.advertiser.create_set()
    config.set = rsp.handle
    assert rsp.result == 0

    rsp = DUT.bt.advertiser.set_timing(config.set,
                                       config.min_interval,
                                       config.max_interval,
                                       config.duration,
                                       config.max_events)
    assert rsp.result == 0
    log.info("DUT Advertiser configured")

    DUT.bt.advertiser.set_tx_power(config.set, config.tx_power)
    
    DUT.bt.extended_advertiser.set_phy(
        config.set,
        config.primary_phy,
        config.secondary_phy,
    )
    log.info(
        "DUT extended advertiser PHY configured: primary=%s secondary=%s",
        advertising_phy_to_str(config.primary_phy),
        advertising_phy_to_str(config.secondary_phy),
    )
    DUT.bt.extended_advertiser.set_data(config.set, config.payload)

    return config

@pytest.fixture
def DUTAdvStart(DUT,
                DUTAdvConfig):
    started = False
    last_connectable_mode = None
    last_flags = None

    def start(*, connectable_mode, flags=0):
        nonlocal started, last_connectable_mode, last_flags
        last_connectable_mode = connectable_mode
        last_flags = flags

        rsp = DUT.bt.extended_advertiser.start(DUTAdvConfig.set, connectable_mode, flags)
        assert rsp.result == 0
        log.info("DUT Advertising started")
        started = True

    def restart():
        if last_connectable_mode is None:
            raise RuntimeError("DUT Advertising cannot be restarted before it has been started")

        try:
            rsp = DUT.bt.advertiser.stop(DUTAdvConfig.set)
            assert rsp.result == 0
            log.info("DUT Advertising stopped before restart")
        except Exception as ex:
            log.debug("DUT Advertising stop before restart skipped: %s", ex)

        rsp = DUT.bt.extended_advertiser.start(DUTAdvConfig.set, last_connectable_mode, last_flags)
        assert rsp.result == 0
        log.info("DUT Advertising restarted")

    start.restart = restart

    yield start

    try:
        if started:
            rsp = DUT.bt.advertiser.stop(DUTAdvConfig.set)
            assert rsp.result == 0
            log.info("DUT Advertising stopped")
        rsp = DUT.bt.advertiser.delete_set(DUTAdvConfig.set)
        assert rsp.result == 0
        log.info("DUT Advertising set deleted")
    except Exception as exc:
        _log_fixture_cleanup_failure("DUT advertiser", exc)

@pytest.fixture
def DUTScanConfig(DUT,
                  Config) -> ScannerConfig:
    def _config(*, mode):
        config = dataclass_from_dict(ScannerConfig, Config.get("scanner",{}))
        config = normalize_scanner_config(config)
        # Configure the scanner
        DUT.bt.scanner.set_parameters(mode,
                                      config.interval,
                                      config.window)
        
        return config

    return _config

@pytest.fixture
def DUTScanStart(DUT,
                 DUTScanConfig):
    started = False
    def _start(*, discover_mode, mode=None, phy=None):
        nonlocal started
        if mode is None:
            mode = getattr(DUT.bt.scanner, "SCAN_MODE_SCAN_MODE_PASSIVE", 0)
        cfg = DUTScanConfig(mode=mode)
        scan_phy = cfg.phy if phy is None else phy
        rsp = DUT.bt.scanner.start(scan_phy, discover_mode)
        assert rsp.result == 0
        log.info(
            "DUT scanning started: phy=%s, raw_phy=%d, discover_mode=%d",
            advertising_phy_to_str(scan_phy),
            scan_phy,
            discover_mode,
        )
        started = True

    yield _start

    if started:
        try:
            DUT.bt.scanner.stop()
            log.info("DUT scanning stopped")
        except Exception as exc:
            _log_fixture_cleanup_failure("DUT scanner", exc)


@pytest.fixture
def DUTPAwRStart(DUT,
                 DUTAdvConfig,
                 Config):
    started = False
    pawr_cfg = dataclass_from_dict(PAWRConfig, Config.get("pawr", {}))

    def _start(*, flags=0):
        nonlocal started
        pawr_payload = DUTAdvConfig.payload

        rsp = DUT.bt.extended_advertiser.set_phy(
            DUTAdvConfig.set,
            DUTAdvConfig.primary_phy,
            DUTAdvConfig.secondary_phy,
        )
        assert rsp.result == 0
        log.info(
            "DUT PAwR advertiser PHY configured: primary=%s secondary=%s",
            advertising_phy_to_str(DUTAdvConfig.primary_phy),
            advertising_phy_to_str(DUTAdvConfig.secondary_phy),
        )

        rsp = start_pawr_advertising(
            DUT,
            DUTAdvConfig.set,
            pawr_cfg,
            flags=flags,
        )
        assert rsp.result == 0

        for subevent in range(pawr_cfg.num_subevents):
            rsp = DUT.bt.pawr_advertiser.set_subevent_data(
                DUTAdvConfig.set,
                subevent,
                0,
                pawr_cfg.response_slots,
                pawr_payload,
            )
            assert rsp.result == 0

        dut_identity = DUT.bt.system.get_identity_address()
        assert dut_identity.result == 0
        log.info(
            "DUT PAwR advertiser started: set=%s identity_address=%s pawr_payload=%sB num_subevents=%s",
            DUTAdvConfig.set,
            _format_bd_addr(dut_identity.address),
            len(pawr_payload),
            pawr_cfg.num_subevents,
        )
        started = True

    yield _start

    try:
        if started:
            rsp = DUT.bt.pawr_advertiser.stop(DUTAdvConfig.set)
            assert rsp.result == 0
            log.info("DUT PAwR advertiser stopped")
        rsp = DUT.bt.advertiser.delete_set(DUTAdvConfig.set)
        assert rsp.result == 0
        log.info("DUT PAwR advertising set deleted")
    except Exception as exc:
        _log_fixture_cleanup_failure("DUT PAwR advertiser", exc)


@pytest.fixture
def DUTScanSync(DUT):
    """Configure DUT PAwR synchronization with a specific advertiser."""
    started = False
    sync_handle = None

    def _start(*, address, address_type, adv_sid=0, skip=0, timeout=200, reporting_mode=None):
        nonlocal started, sync_handle
        if reporting_mode is None:
            reporting_mode = DUT.bt.sync.REPORTING_MODE_REPORT_ALL

        rsp = DUT.bt.sync_scanner.set_sync_parameters(skip, timeout, reporting_mode)
        assert rsp.result == 0
        log.info(
            "DUT sync parameters set: skip=%s timeout=%s reporting_mode=%s",
            skip,
            timeout,
            reporting_mode,
        )

        rsp = DUT.bt.sync_scanner.open(address, address_type, adv_sid)
        assert rsp.result == 0
        sync_handle = rsp.sync
        log.info(
            "DUT sync open requested: sync=%s address=%s address_type=%s adv_sid=%s",
            sync_handle,
            _format_bd_addr(address),
            address_type,
            adv_sid,
        )

        try:
            bgapi_check_event(
                DUT,
                "pawr_sync",
                "opened",
                lambda evt: evt.sync == sync_handle,
                eventTimeout=1,
                maxTime=10,
            )
            log.info("DUT PAwR sync opened")

        except BGAPIEventTimeoutError:
            bgapi_check_event(
                DUT,
                "pawr_sync",
                "subevent_report",
                lambda evt: evt.sync == sync_handle,
                eventTimeout=1,
                maxTime=10,
            )
            log.info("DUT PAwR sync established via subevent reports")

        started = True
        return sync_handle

    yield _start

    try:
        if started and sync_handle is not None:
            DUT.bt.sync.close(sync_handle)
            bgapi_check_event(
                DUT,
                "sync",
                "closed",
                lambda evt: evt.sync == sync_handle,
                eventTimeout=1,
                maxTime=5,
            )
            log.info("DUT PAwR sync closed")
    except Exception as exc:
        _log_fixture_cleanup_failure("DUT PAwR sync", exc)

@pytest.fixture
def TesterAdvConfig(Tester,
                    Config) -> AdvertiserConfig:
    log.info("Configuring Tester Advertiser")

    config = dataclass_from_dict(AdvertiserConfig, Config.get("advertiser",{}))
    config = normalize_advertiser_config(config)

    rsp = Tester.bt.advertiser.create_set()
    config.set = rsp.handle
    assert rsp.result == 0

    rsp = Tester.bt.advertiser.set_timing(config.set,
                                          config.min_interval,
                                          config.max_interval,
                                          config.duration,
                                          config.max_events)
    assert rsp.result == 0
    log.info("Tester Advertiser configured")

    Tester.bt.advertiser.set_tx_power(config.set, config.tx_power)
    Tester.bt.extended_advertiser.set_phy(
        config.set,
        config.primary_phy,
        config.secondary_phy,
    )
    log.info(
        "Tester extended advertiser PHY configured: primary=%s secondary=%s",
        advertising_phy_to_str(config.primary_phy),
        advertising_phy_to_str(config.secondary_phy),
    )
    Tester.bt.extended_advertiser.set_data(config.set, config.payload)

    return config

@pytest.fixture
def TesterAdvStart(Tester,
                   TesterAdvConfig):
    started = False
    last_connectable_mode = None
    last_flags = None

    def start(*, connectable_mode, flags=0):
        nonlocal started, last_connectable_mode, last_flags
        last_connectable_mode = connectable_mode
        last_flags = flags

        rsp = Tester.bt.extended_advertiser.start(TesterAdvConfig.set, connectable_mode, flags)
        assert rsp.result == 0
        log.info("Tester Advertising started")
        started = True

    def restart():
        if last_connectable_mode is None:
            raise RuntimeError("Tester Advertising cannot be restarted before it has been started")

        try:
            rsp = Tester.bt.advertiser.stop(TesterAdvConfig.set)
            assert rsp.result == 0
            log.info("Tester Advertising stopped before restart")
        except Exception as ex:
            log.debug("Tester Advertising stop before restart skipped: %s", ex)

        rsp = Tester.bt.extended_advertiser.start(TesterAdvConfig.set, last_connectable_mode, last_flags)
        assert rsp.result == 0
        log.info("Tester Advertising restarted")

    start.restart = restart

    yield start

    try:
        if started:
            rsp = Tester.bt.advertiser.stop(TesterAdvConfig.set)
            assert rsp.result == 0
            log.info("Tester Advertising stopped")
        rsp = Tester.bt.advertiser.delete_set(TesterAdvConfig.set)
        assert rsp.result == 0
        log.info("Tester Advertising set deleted")
    except Exception as exc:
        _log_fixture_cleanup_failure("Tester advertiser", exc)

@pytest.fixture
def TesterScanConfig(Tester,
                     Config) -> ScannerConfig:
    def _config(*, mode):
        config = dataclass_from_dict(ScannerConfig, Config.get("scanner",{}))
        config = normalize_scanner_config(config)
        # Configure the scanner
        Tester.bt.scanner.set_parameters(mode,
                                         config.interval,
                                         config.window)
        
        return config

    return _config

@pytest.fixture
def TesterScanStart(Tester,
                    TesterScanConfig):
    started = False
    def _start(*, discover_mode, mode=None, phy=None):
        nonlocal started
        if mode is None:
            mode = getattr(Tester.bt.scanner, "SCAN_MODE_SCAN_MODE_PASSIVE", 0)
        cfg = TesterScanConfig(mode=mode)
        scan_phy = cfg.phy if phy is None else phy
        rsp = Tester.bt.scanner.start(scan_phy, discover_mode)
        assert rsp.result == 0
        log.info(
            "Tester scanning started: phy=%s, raw_phy=%d, discover_mode=%d",
            advertising_phy_to_str(scan_phy),
            scan_phy,
            discover_mode,
        )
        started = True

    yield _start

    if started:
        try:
            Tester.bt.scanner.stop()
            log.info("Tester scanning stopped")
        except Exception as exc:
            _log_fixture_cleanup_failure("Tester scanner", exc)

@pytest.fixture
def TesterDiscoverPAwRAdvertiser(Tester,
                                 TesterScanStart):
    raise NotImplementedError("TesterDiscoverPAwRAdvertiser fixture is not implemented yet.")

@pytest.fixture
def TesterScanSync(Tester):
    """Configure tester PAwR synchronization with a specific advertiser."""
    started = False
    sync_handle = None

    def _start(*, address, address_type, adv_sid=0, skip=0, timeout=200, reporting_mode=None):
        nonlocal started, sync_handle
        if reporting_mode is None:
            reporting_mode = Tester.bt.sync.REPORTING_MODE_REPORT_ALL

        rsp = Tester.bt.sync_scanner.set_sync_parameters(skip, timeout, reporting_mode)
        assert rsp.result == 0
        log.info(
            "Tester sync parameters set: skip=%s timeout=%s reporting_mode=%s",
            skip,
            timeout,
            reporting_mode,
        )

        rsp = Tester.bt.sync_scanner.open(address, address_type, adv_sid)
        assert rsp.result == 0
        sync_handle = rsp.sync
        log.info(
            "Tester sync open requested: sync=%s address=%s address_type=%s adv_sid=%s",
            sync_handle,
            _format_bd_addr(address),
            address_type,
            adv_sid,
        )

        try:
            bgapi_check_event(
                Tester,
                "sync",
                "closed",
                lambda evt: _log_sync_event(evt),
                eventTimeout=0.5,
                maxTime=2,
            )
        except BGAPIEventTimeoutError:
            log.info("No sync.closed event observed during initial sync debug window")

        try:
            bgapi_check_event(
                Tester,
                "pawr_sync",
                "opened",
                lambda evt: evt.sync == sync_handle,
                eventTimeout=1,
                maxTime=10,
            )
            log.info("Tester PAwR sync opened")
        except BGAPIEventTimeoutError:
            bgapi_check_event(
                Tester,
                "pawr_sync",
                "subevent_report",
                lambda evt: evt.sync == sync_handle,
                eventTimeout=1,
                maxTime=10,
            )
            log.info("Tester PAwR sync established via subevent reports")

        started = True
        return sync_handle

    yield _start

    try:
        if started and sync_handle is not None:
            Tester.bt.sync.close(sync_handle)
            bgapi_check_event(
                Tester,
                "sync",
                "closed",
                lambda evt: evt.sync == sync_handle,
                eventTimeout=1,
                maxTime=5,
            )
            log.info("Tester PAwR sync closed")
    except Exception as exc:
        _log_fixture_cleanup_failure("Tester PAwR sync", exc)


@pytest.fixture
def TesterPAwRStart(Tester,
                    TesterAdvConfig,
                    Config):
    started = False
    pawr_cfg = dataclass_from_dict(PAWRConfig, Config.get("pawr", {}))

    def _start(*, flags=0):
        nonlocal started
        pawr_payload = TesterAdvConfig.payload

        rsp = Tester.bt.extended_advertiser.set_phy(
            TesterAdvConfig.set,
            TesterAdvConfig.primary_phy,
            TesterAdvConfig.secondary_phy,
        )
        assert rsp.result == 0
        log.info(
            "Tester PAwR advertiser PHY configured: primary=%s secondary=%s",
            advertising_phy_to_str(TesterAdvConfig.primary_phy),
            advertising_phy_to_str(TesterAdvConfig.secondary_phy),
        )

        rsp = start_pawr_advertising(
            Tester,
            TesterAdvConfig.set,
            pawr_cfg,
            flags=flags,
        )
        assert rsp.result == 0

        for subevent in range(pawr_cfg.num_subevents):
            rsp = Tester.bt.pawr_advertiser.set_subevent_data(
                TesterAdvConfig.set,
                subevent,
                0,
                pawr_cfg.response_slots,
                pawr_payload,
            )
            assert rsp.result == 0

        tester_identity = Tester.bt.system.get_identity_address()
        assert tester_identity.result == 0
        log.info(
            "Tester PAwR advertiser started: set=%s identity_address=%s pawr_payload=%sB num_subevents=%s",
            TesterAdvConfig.set,
            _format_bd_addr(tester_identity.address),
            len(pawr_payload),
            pawr_cfg.num_subevents,
        )
        started = True

    yield _start

    try:
        if started:
            rsp = Tester.bt.pawr_advertiser.stop(TesterAdvConfig.set)
            assert rsp.result == 0
            log.info("Tester PAwR advertiser stopped")
        rsp = Tester.bt.advertiser.delete_set(TesterAdvConfig.set)
        assert rsp.result == 0
        log.info("Tester PAwR advertising set deleted")
    except Exception as exc:
        _log_fixture_cleanup_failure("Tester PAwR advertiser", exc)

@pytest.fixture
def ConnectBLEDevices(Config):
    connected = False
    dev_ctrl = None
    dev_per = None
    ctrl_conn, per_conn = None, None
    timeout_s = None
    
    def _connect_devices(*,
                    central, 
                    peripheral,
                    retry_sleep=MAX_TEST_RETRY_WAIT,
                    max_retries=MAX_TEST_RETRIES,
                    restart_advertising=None):
        """
        Connect two BLE devices where the peripheral has started Connectable Advertising,
        with a maximum number of retries
        """
        nonlocal connected, dev_ctrl, dev_per, ctrl_conn, per_conn, timeout_s

        dev_ctrl = central
        dev_per = peripheral
        params = dataclass_from_dict(ConnectionParameters, Config.get("connection", {}))
        params = normalize_connection_parameters(params)
        sync_connection_config_actuals(Config, params)
        timeout_s = float(connection_ms_from_units(params.max_interval) / 1000)
        central_conn, peripheral_conn = connect_devices(
            central,
            peripheral,
            params,
            retry_sleep=retry_sleep,
            max_retries=max_retries,
            connTimeout=max(5 * timeout_s, 10),
            evtTimeout=max(timeout_s / 5, 0.2),
            restart_advertising=restart_advertising,
        )
        
        log.info("DUT and Tester connected")
        ctrl_conn = central_conn
        per_conn = peripheral_conn
        connected = True

        return central_conn, peripheral_conn, params

    yield _connect_devices

    if connected:
        try:
            dev_ctrl.bt.connection.close(ctrl_conn)
            log.info(
                "Waiting up to %.2fs for connection.closed events after close request",
                timeout_s,
            )

            bgapi_check_event(
                    dev_ctrl,
                    "connection",
                    "closed",
                    lambda evt: evt.connection == ctrl_conn,
                    eventTimeout=1,
                    maxTime=timeout_s,
                )
            bgapi_check_event(
                    dev_per,
                    "connection",
                    "closed",
                    lambda evt: evt.connection == per_conn,
                    eventTimeout=1,
                    maxTime=timeout_s,
                )
            log.info("Connection closed succesfully")
        except Exception as exc:
            _log_fixture_cleanup_failure("BLE connection", exc)

def test_legacy_beacon(JoulescopeDev,
                       DUT,
                       Tester,
                       DUTLegAdvConfig,
                       DUTLegAdvStart,
                       TesterScanStart):
    DUTLegAdvStart(
        connectable_mode=DUT.bt.legacy_advertiser.CONNECTION_MODE_NON_CONNECTABLE,
    )
    TesterScanStart(
        discover_mode=Tester.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION,
        phy=_discovery_scan_phy_for_advertiser(DUTLegAdvConfig.phy),
    )

    dut_addr = get_device_addr(DUT)
    timeout_s = float(advertising_ms_from_units(DUTLegAdvConfig.max_interval) / 1000)
    for attempt in range(MAX_TEST_RETRIES):
        log.info("Beacon detection attempt %d/%d", attempt + 1, MAX_TEST_RETRIES)
        try:
            bgapi_check_event(
                Tester,
                "scanner",
                "legacy_advertisement_report",
                lambda evt: evt.address == dut_addr,
                eventTimeout=timeout_s / 5,
                maxTime=timeout_s*5
            )
            break
        except BGAPIEventTimeoutError:
            if attempt == MAX_TEST_RETRIES - 1:
                raise
            sleep(MAX_TEST_RETRY_WAIT)

    n_events = 10
    capture_s = float(advertising_ms_from_units(n_events * DUTLegAdvConfig.max_interval) / 1000)
    log.info(
        "Processing Joulescope profile: %.3f s (%d non-connectable legacy advertising events)",
        capture_s,
        n_events,
    )
    JoulescopeDev.startPowerProfilingSession(capture_s)

def test_beacon(JoulescopeDev,
                DUT,
                Tester,
                DUTAdvConfig,
                DUTAdvStart,
                TesterScanStart):
    DUTAdvStart(
        connectable_mode=DUT.bt.extended_advertiser.CONNECTION_MODE_NON_CONNECTABLE,
    )
    TesterScanStart(
        discover_mode=Tester.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION,
        phy=_discovery_scan_phy_for_advertiser(DUTAdvConfig.primary_phy),
    )

    dut_addr = get_device_addr(DUT)
    timeout_s = float(advertising_ms_from_units(DUTAdvConfig.max_interval) / 1000)
    for attempt in range(MAX_TEST_RETRIES):
        log.info("Beacon detection attempt %d/%d", attempt + 1, MAX_TEST_RETRIES)
        try:
            bgapi_check_event(
                Tester,
                "scanner",
                "extended_advertisement_report",
                lambda evt: evt.address == dut_addr,
                eventTimeout=timeout_s / 5,
                maxTime=timeout_s*5
            )
            break
        except BGAPIEventTimeoutError:
            if attempt == MAX_TEST_RETRIES - 1:
                raise
            sleep(MAX_TEST_RETRY_WAIT)
    n_events = 10
    capture_s = float(advertising_ms_from_units(n_events * DUTAdvConfig.max_interval) / 1000)
    log.info(
        "Running Validation streaming session: %.3f s (%d non-connectable extended advertising events)",
        capture_s,
        n_events,
    )
    JoulescopeDev.startPowerProfilingSession(capture_s)

def test_legacy_conn_adv(JoulescopeDev,
                  DUT,
                  Tester,
                  DUTLegAdvConfig,
                  DUTLegAdvStart,
                  TesterScanStart):
    DUTLegAdvStart(
        connectable_mode=DUT.bt.legacy_advertiser.CONNECTION_MODE_CONNECTABLE,
    )
    TesterScanStart(
        discover_mode=Tester.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION,
        phy=_discovery_scan_phy_for_advertiser(DUTLegAdvConfig.phy),
    )

    dut_addr = get_device_addr(DUT)
    for attempt in range(MAX_TEST_RETRIES):
        log.info("Connectable advertiser detection attempt %d/%d", attempt + 1, MAX_TEST_RETRIES)
        try:
            bgapi_check_event(
                Tester,
                "scanner",
                "legacy_advertisement_report",
                lambda evt: evt.address == dut_addr,
            )
            break
        except BGAPIEventTimeoutError:
            if attempt == MAX_TEST_RETRIES - 1:
                raise
            sleep(MAX_TEST_RETRY_WAIT)

    n_events = 10
    capture_s = float(advertising_ms_from_units(n_events * DUTLegAdvConfig.max_interval) / 1000)
    JoulescopeDev.startPowerProfilingSession(capture_s)

def test_conn_adv(JoulescopeDev,
                  DUT,
                  Tester,
                  DUTAdvConfig,
                  DUTAdvStart,
                  TesterScanStart):
    DUTAdvStart(
        connectable_mode=DUT.bt.extended_advertiser.CONNECTION_MODE_CONNECTABLE,
    )
    TesterScanStart(
        discover_mode=Tester.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION,
        phy=_discovery_scan_phy_for_advertiser(DUTAdvConfig.primary_phy),
    )

    dut_addr = get_device_addr(DUT)
    timeout_s = float(advertising_ms_from_units(DUTAdvConfig.max_interval) / 1000)
    for attempt in range(MAX_TEST_RETRIES):
        log.info("Connectable advertiser detection attempt %d/%d", attempt + 1, MAX_TEST_RETRIES)
        try:
            bgapi_check_event(
                Tester,
                "scanner",
                "extended_advertisement_report",
                lambda evt: evt.address == dut_addr,
                eventTimeout=timeout_s / 5,
                maxTime=timeout_s*5
            )
            break
        except BGAPIEventTimeoutError:
            if attempt == MAX_TEST_RETRIES - 1:
                raise
            sleep(MAX_TEST_RETRY_WAIT)

    n_events = 10
    capture_s = float(advertising_ms_from_units(n_events * DUTAdvConfig.max_interval) / 1000)
    log.info(
        "Processing Joulescope profile: %.3f s (%d connectable extended advertising events)",
        capture_s,
        n_events,
    )
    JoulescopeDev.startPowerProfilingSession(capture_s)

def test_conn_central(JoulescopeDev,
                      DUT,
                      Tester,
                      TesterAdvStart,
                      ConnectBLEDevices):
    TesterAdvStart(
        connectable_mode=Tester.bt.extended_advertiser.CONNECTION_MODE_CONNECTABLE,
    )

    _, _, conn_params = ConnectBLEDevices(
        central=DUT,
        peripheral=Tester,
        restart_advertising=TesterAdvStart.restart,
    )

    n_events = 10
    capture_s = float(connection_ms_from_units(n_events * conn_params.max_interval) / 1000)
    log.info(f"Processing connection profile for {capture_s}s")
    JoulescopeDev.startPowerProfilingSession(capture_s)


def test_conn_peripheral(JoulescopeDev,
                         DUT,
                         Tester,
                         DUTAdvStart,
                         ConnectBLEDevices):
    DUTAdvStart(
        connectable_mode=DUT.bt.extended_advertiser.CONNECTION_MODE_CONNECTABLE,
    )
    
    _, _, conn_params = ConnectBLEDevices(
        central=Tester,
        peripheral=DUT,
        restart_advertising=DUTAdvStart.restart,
    )

    n_events = 10
    capture_s = float(connection_ms_from_units(n_events * conn_params.max_interval) / 1000)
    log.info(f"Processing connection profile for {capture_s}s")
    JoulescopeDev.startPowerProfilingSession(capture_s)


def test_scanner(JoulescopeDev,
                 DUT,
                 Tester,
                 TesterAdvConfig,
                 TesterAdvStart,
                 DUTScanConfig,
                 DUTScanStart):
    TesterAdvStart(
        connectable_mode=Tester.bt.extended_advertiser.CONNECTION_MODE_CONNECTABLE,
    )
    scan_mode = getattr(DUT.bt.scanner, "SCAN_MODE_SCAN_MODE_PASSIVE", 0)
    scan_config = DUTScanConfig(mode=scan_mode)
    DUTScanStart(
        discover_mode=DUT.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION,
        mode=scan_mode,
        phy=scan_config.phy,
    )

    tester_identity = Tester.bt.system.get_identity_address()
    assert tester_identity.result == 0
    expected_address = tester_identity.address
    expected_address_type = tester_identity.type
    log.info(
        "Scanner test discovery config: advertiser_primary_phy=%s advertiser_secondary_phy=%s scanner_phy=%s",
        advertising_phy_to_str(TesterAdvConfig.primary_phy),
        advertising_phy_to_str(TesterAdvConfig.secondary_phy),
        advertising_phy_to_str(scan_config.phy),
    )
    log.info(
        "Scanner test expected advertiser: address=%s address_type=%s",
        _format_bd_addr(expected_address),
        expected_address_type,
    )

    def _match_tester_advertisement(evt) -> bool:
        received_address = getattr(evt, "address", None)
        received_address_type = getattr(evt, "address_type", None)
        log.info(
            "Scanner test received advertisement: address=%s address_type=%s expected_address=%s expected_address_type=%s",
            _format_bd_addr(received_address),
            received_address_type,
            _format_bd_addr(expected_address),
            expected_address_type,
        )
        return (
            received_address == expected_address
            and received_address_type == expected_address_type
        )

    timeout_s = float(advertising_ms_from_units(TesterAdvConfig.max_interval) / 1000)
    bgapi_check_event(
        DUT,
        "scanner",
        "extended_advertisement_report",
        _match_tester_advertisement,
        eventTimeout=max(timeout_s / 5, 0.2),
        maxTime=max(timeout_s * 5, 5),
    )
    log.info("Scanner test expected advertisement received")

    n_events = 10
    capture_s = float(advertising_ms_from_units(n_events * scan_config.interval) / 1000)
    JoulescopeDev.startPowerProfilingSession(capture_s)

def test_PAwR_per(JoulescopeDev,
                  DUT,
                  Tester,
                  DUTAdvConfig,
                  DUTPAwRStart,
                  TesterScanStart,
                  TesterScanSync):
    dut_address = get_device_addr(DUT)

    TesterScanStart(
        discover_mode=Tester.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION,
        phy=_discovery_scan_phy_for_advertiser(DUTAdvConfig.primary_phy),
    )

    DUTPAwRStart(flags=DUT.bt.periodic_advertiser.FLAGS_AUTO_START_EXTENDED_ADVERTISING)
    log.info(
        "DUT extended advertising started for PAwR discovery: set=%s identity_address=%s",
        DUTAdvConfig.set,
        _format_bd_addr(dut_address),
    )

    adv_report = bgapi_check_event(
        Tester,
        "scanner",
        "extended_advertisement_report",
        lambda evt: _match_pawr_advertiser_report(
            evt,
            dut_address,
            require_periodic_interval=False,
        ),
        eventTimeout=1,
        maxTime=10,
    )
    log.info(
        "Tester matched DUT extended advertisement before PAwR start: address=%s address_type=%s adv_sid=%s periodic_interval=%s",
        _format_bd_addr(adv_report.address),
        adv_report.address_type,
        adv_report.adv_sid,
        adv_report.periodic_interval,
    )


    adv_report = bgapi_check_event(
        Tester,
        "scanner",
        "extended_advertisement_report",
        lambda evt: _match_pawr_advertiser_report(
            evt,
            dut_address,
            require_periodic_interval=True,
        ),
        eventTimeout=1,
        maxTime=10,
    )
    log.info(
        "Tester matched DUT PAwR-capable advertisement: address=%s address_type=%s adv_sid=%s periodic_interval=%s",
        _format_bd_addr(adv_report.address),
        adv_report.address_type,
        adv_report.adv_sid,
        adv_report.periodic_interval,
    )
    Tester.bt.scanner.stop()
    log.info("Tester scanning stopped before PAwR sync open")

    TesterScanSync(
        address=adv_report.address,
        address_type=adv_report.address_type,
        adv_sid=adv_report.adv_sid,
    )

    n_events = 10
    capture_s = float(advertising_ms_from_units(n_events * DUTAdvConfig.max_interval) / 1000)
    JoulescopeDev.startPowerProfilingSession(capture_s)


def test_PAwR_obs(JoulescopeDev,
                  DUT,
                  Tester,
                  TesterAdvConfig,
                  DUTScanStart,
                  DUTScanSync,
                  TesterPAwRStart):
    tester_address = get_device_addr(Tester)

    DUTScanStart(
        discover_mode=DUT.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION,
        phy=_discovery_scan_phy_for_advertiser(TesterAdvConfig.primary_phy),
    )

    TesterPAwRStart(flags=Tester.bt.periodic_advertiser.FLAGS_AUTO_START_EXTENDED_ADVERTISING)
    log.info(
        "Tester extended advertising started for PAwR discovery: set=%s identity_address=%s",
        TesterAdvConfig.set,
        _format_bd_addr(tester_address),
    )

    adv_report = bgapi_check_event(
        DUT,
        "scanner",
        "extended_advertisement_report",
        lambda evt: _match_pawr_advertiser_report(
            evt,
            tester_address,
            require_periodic_interval=False,
        ),
        eventTimeout=1,
        maxTime=10,
    )
    log.info(
        "DUT matched Tester extended advertisement before PAwR start: address=%s address_type=%s adv_sid=%s periodic_interval=%s",
        _format_bd_addr(adv_report.address),
        adv_report.address_type,
        adv_report.adv_sid,
        adv_report.periodic_interval,
    )

    adv_report = bgapi_check_event(
        DUT,
        "scanner",
        "extended_advertisement_report",
        lambda evt: _match_pawr_advertiser_report(
            evt,
            tester_address,
            require_periodic_interval=True,
        ),
        eventTimeout=1,
        maxTime=10,
    )
    log.info(
        "DUT matched Tester PAwR-capable advertisement: address=%s address_type=%s adv_sid=%s periodic_interval=%s",
        _format_bd_addr(adv_report.address),
        adv_report.address_type,
        adv_report.adv_sid,
        adv_report.periodic_interval,
    )
    DUT.bt.scanner.stop()
    log.info("DUT scanning stopped before PAwR sync open")

    DUTScanSync(
        address=adv_report.address,
        address_type=adv_report.address_type,
        adv_sid=adv_report.adv_sid,
    )

    n_events = 10
    capture_s = float(advertising_ms_from_units(n_events * TesterAdvConfig.max_interval) / 1000)
    JoulescopeDev.startPowerProfilingSession(capture_s)

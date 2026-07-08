import pytest
import logging
import json
import os
from dataclasses import dataclass
from time import sleep
from bgapi.bglib import BGLib
from bgapi.serialconnector import SerialConnector
from joulescope_interface import JoulescopeDev, JoulescopeUsbReset
from utils.bgapi_utils import *
from conftest import *

# TODO - BGAPI device interfaces in pytest so that we can use the HWMux thingy
MAX_TEST_RETRIES = 3
DUT_OPEN_RETRIES = 3
DUT_OPEN_RETRY_SLEEP_S = 1.0
log = logging.getLogger(__name__)


def _format_bd_addr(address) -> str:
    return str(address)


def _discovery_scan_phy_for_advertiser(phy: str | int) -> int:
    phy = primary_advertising_phy(phy)
    if phy == PHY_CODED:
        return PHY_CODED
    return PHY_1M


def open_ble_dev(dev,
                 wakeUp=None,
                 *,
                 open_retries=2,
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

    raise RuntimeError(
        f"Failed to open/reset BLE device after {open_retries} attempt(s)"
    ) from last_error


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
        JoulescopeDev):
    dut = BLEDev("dut")
    yield open_ble_dev(
        dut,
        wakeUp=JoulescopeDev,
        open_retries=DUT_OPEN_RETRIES,
        retry_sleep_s=DUT_OPEN_RETRY_SLEEP_S,
    )

@pytest.fixture
def Tester(BLEDev):
    tester = BLEDev("tester")
    yield open_ble_dev(tester)

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
    DUT.bt.advertiser.set_phy(config.set, config.primary_phy, config.primary_phy)
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

    if started:
        try:
            DUT.bt.advertiser.stop(DUTLegAdvConfig.set)
            log.info("DUT Advertising stopped")
        finally:
            DUT.bt.advertiser.delete_set(DUTLegAdvConfig.set)
            log.info("DUT Advertising set deleted")
    else:
        # even if never started, still delete the set
        DUT.bt.advertiser.delete_set(DUTLegAdvConfig.set)

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

    def start(*, connectable_mode, flags=0):
        nonlocal started

        rsp = DUT.bt.extended_advertiser.start(DUTAdvConfig.set, connectable_mode, flags)
        assert rsp.result == 0
        log.info("DUT Advertising started")
        started = True

    yield start

    if started:
        try:
            rsp = DUT.bt.advertiser.stop(DUTAdvConfig.set)
            assert rsp.result == 0
            log.info("DUT Advertising stopped")
        finally:
            rsp = DUT.bt.advertiser.delete_set(DUTAdvConfig.set)
            assert rsp.result == 0
            log.info("DUT Advertising set deleted")
    else:
        # even if never started, still delete the set
        rsp = DUT.bt.advertiser.delete_set(DUTAdvConfig.set)
        assert rsp.result == 0

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
        DUT.bt.scanner.start(scan_phy, discover_mode)
        log.info("DUT scanning started: phy=%s", advertising_phy_to_str(scan_phy))
        started = True

    yield _start

    if started:
        DUT.bt.scanner.stop()
        log.info("DUT scanning stopped")


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

        rsp = DUT.bt.pawr_advertiser.start(
            DUTAdvConfig.set,
            DUTAdvConfig.min_interval,
            DUTAdvConfig.max_interval,
            flags,
            pawr_cfg.num_subevents,
            pawr_cfg.subevent_interval,
            pawr_cfg.response_slot_delay,
            pawr_cfg.response_slot_spacing,
            pawr_cfg.response_slots,
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

    if started:
        try:
            rsp = DUT.bt.pawr_advertiser.stop(DUTAdvConfig.set)
            assert rsp.result == 0
            log.info("DUT PAwR advertiser stopped")
        finally:
            rsp = DUT.bt.advertiser.delete_set(DUTAdvConfig.set)
            assert rsp.result == 0
            log.info("DUT PAwR advertising set deleted")
    else:
        rsp = DUT.bt.advertiser.delete_set(DUTAdvConfig.set)
        assert rsp.result == 0


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

    def start(*, connectable_mode, flags=0):
        nonlocal started

        rsp = Tester.bt.extended_advertiser.start(TesterAdvConfig.set, connectable_mode, flags)
        assert rsp.result == 0
        log.info("Tester Advertising started")
        started = True

    yield start

    if started:
        try:
            rsp = Tester.bt.advertiser.stop(TesterAdvConfig.set)
            assert rsp.result == 0
            log.info("Tester Advertising stopped")
        finally:
            rsp = Tester.bt.advertiser.delete_set(TesterAdvConfig.set)
            assert rsp.result == 0
            log.info("Tester Advertising set deleted")
    else:
        rsp = Tester.bt.advertiser.delete_set(TesterAdvConfig.set)
        assert rsp.result == 0

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
        Tester.bt.scanner.start(scan_phy, discover_mode)
        log.info("Tester scanning started: phy=%s", advertising_phy_to_str(scan_phy))
        started = True

    yield _start

    if started:
        Tester.bt.scanner.stop()
        log.info("Tester scanning stopped")

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

        rsp = Tester.bt.pawr_advertiser.start(
            TesterAdvConfig.set,
            TesterAdvConfig.min_interval,
            TesterAdvConfig.max_interval,
            flags,
            pawr_cfg.num_subevents,
            pawr_cfg.subevent_interval,
            pawr_cfg.response_slot_delay,
            pawr_cfg.response_slot_spacing,
            pawr_cfg.response_slots,
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

    if started:
        try:
            rsp = Tester.bt.pawr_advertiser.stop(TesterAdvConfig.set)
            assert rsp.result == 0
            log.info("Tester PAwR advertiser stopped")
        finally:
            rsp = Tester.bt.advertiser.delete_set(TesterAdvConfig.set)
            assert rsp.result == 0
            log.info("Tester PAwR advertising set deleted")
    else:
        rsp = Tester.bt.advertiser.delete_set(TesterAdvConfig.set)
        assert rsp.result == 0

@pytest.fixture
def ConnectBLEDevices(Config):
    connected = False
    dev_ctrl = None
    dev_per = None
    ctrl_conn, per_conn = None, None
    timeout = None
    
    def _connect_devices(*,
                    central, 
                    peripheral,
                    retry_sleep=0.25,
                    max_retries=3):
        """
        Connect two BLE devices where the peripheral has started Connectable Advertising,
        with a maximum number of retries
        """
        nonlocal connected, dev_ctrl, dev_per, ctrl_conn, per_conn, timeout

        dev_ctrl = central
        dev_per = peripheral
        params = dataclass_from_dict(ConnectionParameters, Config.get("connection", {}))
        params = normalize_connection_parameters(params)
        timeout = max(5.0, params.timeout / 100.0)

        central_conn, peripheral_conn = connect_devices(
            central,
            peripheral,
            params,
            retry_sleep=retry_sleep,
            max_retries=max_retries,
            connTimeout=timeout)
        
        log.info("DUT and Tester connected")
        ctrl_conn = central_conn
        per_conn = peripheral_conn
        connected = True

        return central_conn, peripheral_conn, params

    yield _connect_devices

    if connected:
        dev_ctrl.bt.connection.close(ctrl_conn)

        bgapi_check_event(
                dev_ctrl,
                "connection",
                "closed",
                lambda evt: evt.connection == ctrl_conn,
                eventTimeout=1,
                maxTime=timeout,
            )
        bgapi_check_event(
                dev_per,
                "connection",
                "closed",
                lambda evt: evt.connection == per_conn,
                eventTimeout=1,
                maxTime=timeout,
            )
        log.info("Connection closed succesfully")

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
            sleep(0.25)

    n_events = 10
    capture_s = float(advertising_ms_from_units(n_events * DUTLegAdvConfig.max_interval) / 1000)
    log.info(
        "Capturing Joulescope profile: %.3f s (%d non-connectable legacy advertising events)",
        capture_s,
        n_events,
    )
    JoulescopeDev.capture(capture_s)

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
            sleep(0.25)
    n_events = 10
    capture_s = float(advertising_ms_from_units(n_events * DUTAdvConfig.max_interval) / 1000)
    log.info(
        "Capturing Joulescope profile: %.3f s (%d non-connectable extended advertising events)",
        capture_s,
        n_events,
    )
    JoulescopeDev.capture(capture_s)

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
            sleep(0.25)

    n_events = 10
    capture_s = float(advertising_ms_from_units(n_events * DUTLegAdvConfig.max_interval) / 1000)
    JoulescopeDev.capture(capture_s)

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
            sleep(0.25)

    n_events = 10
    capture_s = float(advertising_ms_from_units(n_events * DUTAdvConfig.max_interval) / 1000)
    log.info(
        "Capturing Joulescope profile: %.3f s (%d connectable extended advertising events)",
        capture_s,
        n_events,
    )
    JoulescopeDev.capture(capture_s)

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
        peripheral=Tester
    )

    n_events = 10
    capture_s = float(connection_ms_from_units(n_events * conn_params.max_interval) / 1000)
    log.info(f"Capturing Conn Adv for {capture_s}s")
    JoulescopeDev.capture(capture_s)


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
        peripheral=DUT
    )

    n_events = 10
    capture_s = float(connection_ms_from_units(n_events * conn_params.max_interval) / 1000)
    log.info(f"Capturing Conn Adv for {capture_s}s")
    JoulescopeDev.capture(capture_s)


def test_scanner(JoulescopeDev,
                 DUT,
                 Tester,
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
    )

    n_events = 10
    capture_s = float(advertising_ms_from_units(n_events * scan_config.interval) / 1000)
    JoulescopeDev.capture(capture_s)

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
    JoulescopeDev.capture(capture_s)


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
    JoulescopeDev.capture(capture_s)

import pytest
import logging
import json
import os
from dataclasses import dataclass
from time import sleep
from bgapi.bglib import BGLib
from bgapi.serialconnector import SerialConnector
from joulescope_interface import JoulescopeDev
from utils.bgapi_utils import *
from conftest import *

# TODO - BGAPI device interfaces in pytest so that we can use the HWMux thingy
MAX_TEST_RETRIES = 3
log = logging.getLogger(__name__)

@pytest.fixture
def BLEDev():
    created =[]

    def _create_ble_dev(device : str):
        port= read_dev_config(device, "serial_port" )

        if port is None:
            raise RuntimeError("No serial_port found")

        dev = get_ble_dev(port)

        dev.open()
        dev.bt.system.reset(0)
        sleep(0.5)
        
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
def DUT(BLEDev):
    yield BLEDev("dut")

@pytest.fixture
def Tester(BLEDev):
    yield BLEDev("tester")

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
    DUT.bt.extended_advertiser.set_phy(config.set, config.phy, config.phy)
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
    def _start(*, discover_mode, mode=None):
        nonlocal started
        if mode is None:
            mode = getattr(DUT.bt.scanner, "SCAN_MODE_SCAN_MODE_PASSIVE", 0)
        cfg = DUTScanConfig(mode=mode)
        DUT.bt.scanner.start(cfg.phy, discover_mode)
        log.info("DUT scanning started")
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

    def _start(flags=0):
        nonlocal started

        rsp = DUT.bt.extended_advertiser.set_phy(DUTAdvConfig.set,
                                                 DUTAdvConfig.phy,
                                                 DUTAdvConfig.phy)
        assert rsp.result == 0

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
        log.info("DUT PAwR advertiser started")
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
    Tester.bt.extended_advertiser.set_phy(config.set, config.phy, config.phy)
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
    def _start(*, discover_mode, mode=None):
        nonlocal started
        if mode is None:
            mode = getattr(Tester.bt.scanner, "SCAN_MODE_SCAN_MODE_PASSIVE", 0)
        cfg = TesterScanConfig(mode=mode)
        Tester.bt.scanner.start(cfg.phy, discover_mode)
        log.info("Tester scanning started")
        started = True

    yield _start

    if started:
        Tester.bt.scanner.stop()
        log.info("Tester scanning stopped")

@pytest.fixture
def TesterScanSync(Tester):
    """Configure tester PAwR synchronization with a specific advertiser."""
    started = False
    sync_handle = None

    def _start(*, address, address_type, adv_sid=0, skip=0, timeout=200, reporting_mode=None):
        nonlocal started, sync_handle

        if reporting_mode is None:
            reporting_mode = Tester.bt.periodic_sync.REPORTING_MODE_REPORT_ALL

        rsp = Tester.bt.sync_scanner.set_sync_parameters(skip, timeout, reporting_mode)
        assert rsp.result == 0

        rsp = Tester.bt.sync_scanner.open(address, address_type, adv_sid)
        assert rsp.result == 0
        sync_handle = rsp.sync

        bgapi_check_event(
            Tester,
            "pawr_sync",
            "opened",
            lambda evt: evt.sync == sync_handle,
            eventTimeout=1,
            maxTime=10,
        )

        log.info("Tester PAwR sync opened")
        started = True
        return sync_handle

    yield _start

    if started and sync_handle is not None:
        Tester.bt.periodic_sync.close(sync_handle)
        bgapi_check_event(
            Tester,
            "periodic_sync",
            "closed",
            lambda evt: evt.sync == sync_handle,
            eventTimeout=1,
            maxTime=5,
        )
        log.info("Tester PAwR sync closed")

@pytest.fixture
def ConnectBLEDevices(Config):
    connected = False
    dev_ctrl = None
    dev_per = None
    ctrl_conn, per_conn = None, None

    def _connect_devices(*,
                    central, 
                    peripheral,
                    retry_sleep=0.25,
                    max_retries=3):
        """
        Connect two BLE devices where the peripheral has started Connectable Advertising,
        with a maximum number of retries
        """
        nonlocal connected, dev_ctrl, dev_per, ctrl_conn, per_conn

        dev_ctrl = central
        dev_per = peripheral
        params = dataclass_from_dict(ConnectionParameters, Config.get("connection", {}))
        params = normalize_connection_parameters(params)

        central_conn, peripheral_conn = connect_devices(
            central,
            peripheral,
            params,
            retry_sleep=retry_sleep,
            max_retries=max_retries)
        
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
                maxTime=5,
            )
        bgapi_check_event(
                dev_per,
                "connection",
                "closed",
                lambda evt: evt.connection == per_conn,
                eventTimeout=1,
                maxTime=5,
            )
        log.info("Connection closed succesfully")


def test_beacon(JoulescopeDev,
                DUT,
                Tester,
                DUTAdvConfig,
                DUTAdvStart,
                TesterScanStart):
    DUTAdvStart(
        connectable_mode=DUT.bt.legacy_advertiser.CONNECTION_MODE_NON_CONNECTABLE,
    )
    TesterScanStart(
        discover_mode=Tester.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION
    )

    dut_addr = get_device_addr(DUT)
    # Check the Advertising Packets with the Tester, since the nature of the test
    # is heavily dependant on the environment repeat 3 times if failed
    for attempt in range(MAX_TEST_RETRIES):
        log.info("Beacon detection attempt %d/%d", attempt + 1, MAX_TEST_RETRIES)
        event = bgapi_check_event(Tester,
                                  "scanner",
                                  "extended_advertisement_report",
                                  lambda evt: (evt.address == dut_addr))
    
        if event is None and attempt == MAX_TEST_RETRIES - 1:
            pytest.fail("Test retry maximum reached. Test failed.")
        else:
            break

    capture_s = float(advertising_ms_from_units(10 * DUTAdvConfig.max_interval) / 1000)
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
        discover_mode=Tester.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION
    )

    dut_addr = get_device_addr(DUT)
    for attempt in range(MAX_TEST_RETRIES):
        log.info("Connectable advertiser detection attempt %d/%d", attempt + 1, MAX_TEST_RETRIES)
        event = bgapi_check_event(Tester,
                                  "scanner",
                                  "extended_advertisement_report",
                                  lambda evt: (evt.address == dut_addr))

        if event is None and attempt == MAX_TEST_RETRIES - 1:
            pytest.fail("Test retry maximum reached. Test failed.")
        else:
            break

    n_events = 10
    capture_s = float(advertising_ms_from_units(n_events * DUTAdvConfig.max_interval) / 1000)
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

def test_PAwR_obs(DUT,
                  Tester,
                  DUTPAwRStart,
                  TesterScanSync):
    dut_address = get_device_addr(DUT)

    TesterScanSync(
        address=dut_address,
        address_type=Tester.bt.gap.ADDRESS_TYPE_PUBLIC_ADDRESS
    )



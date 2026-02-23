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
# TODO - BGAPI stuff with pytest so that the tests can use
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

    return config

@pytest.fixture
def DUTAdvStart(DUT,
                DUTAdvConfig):
    started = False

    def start(*, discover_mode, connectable_mode):
        nonlocal started
        rsp = DUT.bt.legacy_advertiser.generate_data(DUTAdvConfig.set, discover_mode)
        assert rsp.result == 0

        # Start advertising and scanning
        rsp = DUT.bt.legacy_advertiser.start(DUTAdvConfig.set, connectable_mode)
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
        # Configure the scanner
        DUT.bt.scanner.set_parameters(mode,
                                      config.interval,
                                      config.window)

    return _config

@pytest.fixture
def DUTScanStart(DUT):
    started = False
    def _start(*, phy, discover_mode):
        nonlocal started
        DUT.bt.scanner.start(phy, discover_mode)
        log.info("DUT scanning started")
        started = True

    yield _start

    if started:
        DUT.bt.scanner.stop()
        log.info("DUT scanning stopped")

@pytest.fixture
def TesterAdvConfig(Tester,
                    Config) -> AdvertiserConfig:
    log.info("Configuring Tester Advertiser")

    config = dataclass_from_dict(AdvertiserConfig, Config.get("advertiser",{}))

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

    return config

@pytest.fixture
def TesterAdvStart(Tester,
                   TesterAdvConfig):
    started = False

    def start(*, discover_mode, connectable_mode):
        nonlocal started
        rsp = Tester.bt.legacy_advertiser.generate_data(TesterAdvConfig.set, discover_mode)
        assert rsp.result == 0

        rsp = Tester.bt.legacy_advertiser.start(TesterAdvConfig.set, connectable_mode)
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
        # Configure the scanner
        DUT.bt.scanner.set_parameters(mode,
                                      config.interval,
                                      config.window)

    return _config

@pytest.fixture
def TesterScanStart(Tester,
                    TesterScanConfig):
    started = False
    def _start(*, phy, discover_mode):
        nonlocal started
        Tester.bt.scanner.start(phy, discover_mode)
        log.info("Tester scanning started")
        started = True

    yield _start

    if started:
        Tester.bt.scanner.stop()
        log.info("Tester scanning stopped")

@pytest.fixture
def ConnectBLEDevices(Config):
    connected = False
    dev_ctrl = None
    dev_per = None
    ctrl_conn, per_conn = None, None

    def _connect_devices(*,
                    central, 
                    peripheral,
                    phy,
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

        central_conn, peripheral_conn = connect_devices(
            central,
            peripheral,
            phy,
            params,
            retry_sleep=retry_sleep,
            max_retries=max_retries)
        
        log.info("DUT and Tester connected")
        ctrl_conn = central_conn
        per_conn = peripheral_conn
        connected = True

        return central_conn, peripheral_conn

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
                DUTAdvStart,
                TesterScanStart):
    DUTAdvStart(
        discover_mode=DUT.bt.advertiser.DISCOVERY_MODE_GENERAL_DISCOVERABLE,
        connectable_mode=DUT.bt.legacy_advertiser.CONNECTION_MODE_NON_CONNECTABLE,
    )
    TesterScanStart(
        phy=Tester.bt.scanner.SCAN_PHY_SCAN_PHY_1M,
        discover_mode=Tester.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION
    )

    dut_addr = get_device_addr(DUT)

    # Check the Advertising Packets with the Tester, since the nature of the test
    # is heavily dependant on the environment repeat 3 times if failed
    for attempt in range(MAX_TEST_RETRIES):
        log.info("Beacon detection attempt %d/%d", attempt + 1, MAX_TEST_RETRIES)
        event = bgapi_check_event(Tester,
                                  "scanner",
                                  "legacy_advertisement_report",
                                  lambda evt: (evt.address == dut_addr))
    
        if event is None and attempt == MAX_TEST_RETRIES - 1:
            pytest.fail("Test retry maximum reached. Test failed.")
        else:
            break

    # Run the capture and store it
    JoulescopeDev.capture(0.2)

def test_connAdv_central(JoulescopeDev,
                         DUT,
                         Tester,
                         TesterAdvStart,
                         ConnectBLEDevices):
    TesterAdvStart(
        discover_mode=Tester.bt.advertiser.DISCOVERY_MODE_GENERAL_DISCOVERABLE,
        connectable_mode=Tester.bt.legacy_advertiser.CONNECTION_MODE_CONNECTABLE,
    )
    ConnectBLEDevices(
        central=DUT,
        peripheral=Tester,
        phy=DUT.bt.gap.PHY_CODING_PHY_CODING_1M_UNCODED
    )

    # TODO - Calculate the time required for N events to complete
    # TODO - Capture with Joulescope on that NEvent time
    JoulescopeDev.capture(0.2)


def test_connAdv_peripheral(JoulescopeDev,
                            DUT,
                            Tester,
                            DUTAdvStart,
                            ConnectBLEDevices):
    DUTAdvStart(
        discover_mode=DUT.bt.advertiser.DISCOVERY_MODE_GENERAL_DISCOVERABLE,
        connectable_mode=DUT.bt.legacy_advertiser.CONNECTION_MODE_CONNECTABLE,
    )
    ConnectBLEDevices(
        central=Tester,
        peripheral=DUT,
        phy=Tester.bt.gap.PHY_CODING_PHY_CODING_1M_UNCODED
    )

    # TODO - Calculate the time required for N events to complete
    # TODO - Capture with Joulescope on that NEvent time
    JoulescopeDev.capture(0.2)


# TODO - Scanning
def test_scanner(JoulescopeDev,
                 DUT,
                 Tester,
                 TesterAdvStart,
                 DUTScanStart):
    TesterAdvStart(
        discover_mode=Tester.bt.advertiser.DISCOVERY_MODE_GENERAL_DISCOVERABLE,
        connectable_mode=Tester.bt.legacy_advertiser.CONNECTION_MODE_CONNECTABLE,
    )
    DUTScanStart(
        phy=DUT.bt.scanner.SCAN_PHY_SCAN_PHY_1M,
        discover_mode=DUT.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION
    )

    # TODO - Calculate the time required for N events to complete
    # TODO - Capture with Joulescope on that NEvent time
    JoulescopeDev.capture(0.2)

# TODO - PAwR Observer
def test_PAwR_obs(DUT):
    assert 1 == 0

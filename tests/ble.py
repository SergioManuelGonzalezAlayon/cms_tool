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
def DUTScanStart(DUT):
    rsp = DUT.bt.scanner.start(DUT.bt.scanner.SCAN_PHY_SCAN_PHY_1M,
                                     DUT.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION)
    assert rsp.result == 0
    log.info("DUT scanning started")

    yield 

    rsp = DUT.bt.scanner.stop()
    assert rsp.result == 0
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
    config = dataclass_from_dict(ScannerConfig, Config.get("scanner",{}))

    # Configure the scanner
    rsp = Tester.bt.scanner.set_parameters(Tester.bt.scanner.SCAN_MODE_SCAN_MODE_PASSIVE,
                                            config.interval,
                                            config.window)
    assert rsp.result == 0 

    return config

@pytest.fixture
def TesterScanStart(Tester,
                    TesterScanConfig):
    rsp = Tester.bt.scanner.start(Tester.bt.scanner.SCAN_PHY_SCAN_PHY_1M,
                                  Tester.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION)
    assert rsp.result == 0
    log.info("Tester scanning started")

    yield 

    rsp = Tester.bt.scanner.stop()
    assert rsp.result == 0
    log.info("Tester scanning stopped")

@pytest.fixture
def TesterOpenConnection(Tester):
    connHandle = -1
    # Open connection with DUT

    yield connHandle

    # Close connection with DUT

def test_beacon(#JoulescopeDev,
                DUT,
                Tester,
                DUTAdvConfig,
                TesterScanConfig,
                DUTAdvStart,
                TesterScanStart):
    DUTAdvStart(
        discover_mode=DUT.bt.advertiser.DISCOVERY_MODE_GENERAL_DISCOVERABLE,
        connectable_mode=DUT.bt.legacy_advertiser.CONNECTION_MODE_NON_CONNECTABLE,
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
    #JoulescopeDev.capture(0.2)


# TODO - Connectable Advertising - Central
def test_connAdv_central(DUT,
                         Tester,
                         TesterAdvStart):
    TesterAdvStart(
        discover_mode=Tester.bt.advertiser.DISCOVERY_MODE_GENERAL_DISCOVERABLE,
        connectable_mode=Tester.bt.legacy_advertiser.CONNECTION_MODE_CONNECTABLE,
    )

    dut_addr = get_device_addr(DUT)
    tester_addr = get_device_addr(Tester)

    DUT.bt.connection.open(
        tester_addr,
        DUT.bt.gap.ADDRESS_TYPE_PUBLIC_ADDRESS,
        DUT.bt.gap.PHY_CODING_PHY_CODING_1M_UNCODED
    )

    dut_conn_evt = bgapi_check_event(
        DUT,
        "connection",
        "opened",
        lambda evt: evt.address == tester_addr and evt.connection != 0,
        eventTimeout=1,
        maxTime=10,
    )

    tester_conn_evt = bgapi_check_event(
        Tester,
        "connection",
        "opened",
        lambda evt: evt.address == dut_addr and evt.connection != 0,
        eventTimeout=1,
        maxTime=10,
    )

    # Sleep for 500ms to allow the connection for the idle state
    sleep(0.5)

    DUT.bt.connection.close(dut_conn_evt.connection)

    conn_closed_evt = bgapi_check_event(
        DUT,
        "connection",
        "closed",
        lambda evt: evt.connection == dut_conn_evt.connection,
        eventTimeout=1,
        maxTime=10
    )

    conn_closed_evt = bgapi_check_event(
        Tester,
        "connection",
        "closed",
        lambda evt: evt.connection == tester_conn_evt.connection,
        eventTimeout=1,
        maxTime=10
    )

# TODO - Connectable Advertising - Peripheral
def test_connAdv_peripheral(DUT,
                            Tester,
                            DUTAdvStart):
    DUTAdvStart(
        discover_mode=DUT.bt.advertiser.DISCOVERY_MODE_GENERAL_DISCOVERABLE,
        connectable_mode=DUT.bt.legacy_advertiser.CONNECTION_MODE_CONNECTABLE,
    )

    dut_addr = get_device_addr(DUT)
    tester_addr = get_device_addr(Tester)

    rsp = Tester.bt.connection.open(
        dut_addr,
        Tester.bt.gap.ADDRESS_TYPE_PUBLIC_ADDRESS,
        Tester.bt.gap.PHY_CODING_PHY_CODING_1M_UNCODED
    )

    tester_conn_evt = bgapi_check_event(Tester,
                                 "connection",
                                 "opened",
                                 lambda evt: evt.address == dut_addr,
                                 eventTimeout=1,
                                 maxTime=10,
                                 )
    assert tester_conn_evt.connection != 0

    dut_conn_evt = bgapi_check_event(DUT,
                                 "connection",
                                 "opened",
                                 lambda evt: evt.address == tester_addr,
                                 eventTimeout=1,
                                 maxTime=10,
                                 )
    assert dut_conn_evt.connection != 0

    # Sleep for 500ms to allow the connection for the idle state
    sleep(0.5)

    rsp = Tester.bt.connection.close(tester_conn_evt.connection)

    conn_closed_evt = bgapi_check_event(
        Tester,
        "connection",
        "closed",
        lambda evt: evt.connection == tester_conn_evt.connection,
        eventTimeout=1,
        maxTime=10
    )
    assert conn_closed_evt is not None

    conn_closed_evt = bgapi_check_event(
        DUT,
        "connection",
        "closed",
        lambda evt: evt.connection == dut_conn_evt.connection,
        eventTimeout=1,
        maxTime=10
    )
    assert conn_closed_evt is not None

# TODO - Scanning
def test_scanner(DUT,
                 Tester,
                 TesterAdvStart,
                 DUTScanStart):
    assert 1 == 0

# TODO - PAwR Observer
def test_PAwR_obs(DUT):
    assert 1 == 0

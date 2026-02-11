import pytest
import logging
import json
import os
from dataclasses import dataclass
from time import sleep
from bgapi.bglib import BGLib
from bgapi.serialconnector import SerialConnector
from joulescope_interface import JoulescopeDev

# TODO - BGAPI device interfaces in pytest so that we can use the HWMux thingy
# TODO - BGAPI stuff with pytest so that the tests can use
BGAPI_SDK_PATH = "C:\\Users\\sagonzal\\SimplicityStudio\\SDKs\\simplicity_sdk\\protocol\\bluetooth\\api\\sl_bt.xapi"
MAX_TEST_RETRIES = 3
log = logging.getLogger(__name__)

def read_dev_config(section, key, default=None, *, path="dev_config.json"):
    """
    Read a value from the dev config JSON file.
    """
    config_path = os.path.join(os.getcwd(), path)
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get(section, {}).get(key, default)

def get_ble_dev(serialPort):
    try:
        dev = BGLib(
            SerialConnector(serialPort),
            BGAPI_SDK_PATH)

        return dev
    except Exception as e:
        log.error(f"Cannot create the BGAPI interface with the device. Reason : {e}")
        pytest.fail()

@pytest.fixture
def BLEDut():
    dut_port= read_dev_config(
        "dut",
        "serial_port"
    )

    dev = get_ble_dev(dut_port)

    try:
        dev.open()
        dev.bt.system.reset(0)
        sleep(0.5)

        yield dev

        dev.close()

    except Exception as e:
        log.error(f"{e}")
        pytest.fail()

@pytest.fixture
def BLETester():
    tester_port= read_dev_config(
        "tester",
        "serial_port"
    )

    dev = get_ble_dev(tester_port)

    try:
        dev.open()
        dev.bt.system.reset(0)
        sleep(0.5)

        yield dev

        dev.close()

    except Exception as e:
        log.error(f"{e}")

@dataclass
class AdvertiserConfig:
    set: int = -1
    min_interval: int = 100
    max_interval: int = 250
    payload: int = 20
    duration: int = 0               # Default value : no duration limit
    max_events: int = 0             # Default value : no event limit

@dataclass
class ScannerConfig:
    window: int = 50      # 50 * 0.625 ms = 31.25 ms
    interval: int = 100   # 100 * 0.625 ms = 62.5 ms

@dataclass
class ConnectionParameters:
    min_interval : int = 100
    max_interval : int = 250
    # TODO - Fill all the Connection Parameters for the dataclass

@pytest.fixture
def DUTAdvConfig(BLEDut) -> AdvertiserConfig:
    log.info("Configuring DUT Advertiser")

    config = AdvertiserConfig()

    rsp = BLEDut.bt.advertiser.create_set()
    config.set = rsp.handle
    assert rsp.result == 0

    rsp = BLEDut.bt.advertiser.set_timing(config.set,
                                          config.min_interval,
                                          config.max_interval,
                                          config.duration,
                                          config.max_events)
    assert rsp.result == 0
    log.info("DUT Advertiser configured")    

    return config

@pytest.fixture
def DUTAdvStart(BLEDut,
                DUTAdvConfig):
    started = False

    def start(*, discover_mode, connectable_mode):
        nonlocal started
        rsp = BLEDut.bt.legacy_advertiser.generate_data(DUTAdvConfig.set, discover_mode)
                                                        # BLEDut.bt.advertiser.DISCOVERY_MODE_GENERAL_DISCOVERABLE)
        assert rsp.result == 0

        # Start advertising and scanning
        rsp = BLEDut.bt.legacy_advertiser.start(DUTAdvConfig.set, connectable_mode)
                                                # BLEDut.bt.legacy_advertiser.CONNECTION_MODE_NON_CONNECTABLE)
        assert rsp.result == 0
        log.info("DUT Advertising started")
        started = True

    yield start

    if started:
        try:
            rsp = BLEDut.bt.advertiser.stop(DUTAdvConfig.set)
            assert rsp.result == 0
            log.info("DUT Advertising stopped")
        finally:
            rsp = BLEDut.bt.advertiser.delete_set(DUTAdvConfig.set)
            assert rsp.result == 0
            log.info("DUT Advertising set deleted")
    else:
        # even if never started, still delete the set
        rsp = BLEDut.bt.advertiser.delete_set(DUTAdvConfig.set)
        assert rsp.result == 0


@pytest.fixture
def TesterScanConfig(BLETester) -> ScannerConfig:
    config = ScannerConfig()

    # Configure the scanner
    rsp = BLETester.bt.scanner.set_parameters(BLETester.bt.scanner.SCAN_MODE_SCAN_MODE_PASSIVE,
                                            config.interval,
                                            config.window)
    assert rsp.result == 0 

    return config

@pytest.fixture
def TesterAdvConfig(BLETester) -> AdvertiserConfig:
    log.info("Configuring Tester Advertiser")

    config = AdvertiserConfig()

    rsp = BLETester.bt.advertiser.create_set()
    config.set = rsp.handle
    assert rsp.result == 0

    rsp = BLETester.bt.advertiser.set_timing(config.set,
                                             config.min_interval,
                                             config.max_interval,
                                             config.duration,
                                             config.max_events)
    assert rsp.result == 0
    log.info("Tester Advertiser configured")

    return config

@pytest.fixture
def TesterAdvStart(BLETester,
                   TesterAdvConfig):
    started = False

    def start(*, discover_mode, connectable_mode):
        nonlocal started
        rsp = BLETester.bt.legacy_advertiser.generate_data(TesterAdvConfig.set, discover_mode)
        assert rsp.result == 0

        rsp = BLETester.bt.legacy_advertiser.start(TesterAdvConfig.set, connectable_mode)
        assert rsp.result == 0
        log.info("Tester Advertising started")
        started = True

    yield start

    if started:
        try:
            rsp = BLETester.bt.advertiser.stop(TesterAdvConfig.set)
            assert rsp.result == 0
            log.info("Tester Advertising stopped")
        finally:
            rsp = BLETester.bt.advertiser.delete_set(TesterAdvConfig.set)
            assert rsp.result == 0
            log.info("Tester Advertising set deleted")
    else:
        rsp = BLETester.bt.advertiser.delete_set(TesterAdvConfig.set)
        assert rsp.result == 0

@pytest.fixture
def TesterScanStart(BLETester):
    rsp = BLETester.bt.scanner.start(BLETester.bt.scanner.SCAN_PHY_SCAN_PHY_1M,
                                     BLETester.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION)
    assert rsp.result == 0
    log.info("Tester scanning started")

    yield 

    rsp = BLETester.bt.scanner.stop()
    assert rsp.result == 0
    log.info("Tester scanning stopped")

@pytest.fixture
def TesterOpenConnection(BLETester):
    connHandle = -1
    # Open connection with DUT

    yield connHandle

    # Close connection with DUT

def bgapi_check_event(dev,
                      event_class,
                      event_name,
                      condition,
                      eventTimeout=0,
                      maxTime=5):
    """
    Check a BGAPI event and return the event itself if there is
    any processing required for it.
    
    :param dev: Description
    :param event_class: Description
    :param event_name: Description
    :param condition: Description
    :param eventTimeout: Description
    :param maxTime: Description
    """
    for evt in dev.get_events(timeout=eventTimeout, max_time=maxTime):
        if evt._class_name == event_class and evt._msg_name == event_name:
            if condition(evt):
                log.info(f"{event_class}_{event_name} found")
                return evt

    return None 

def get_device_addr(dev):
    rsp = dev.bt.system.get_identity_address()
    assert rsp.result == 0
    return rsp.address

def test_beacon(BLEDut,
                BLETester,
                DUTAdvConfig,
                TesterScanConfig,
                DUTAdvStart,
                TesterScanStart):
    DUTAdvStart(
        discover_mode=BLEDut.bt.advertiser.DISCOVERY_MODE_GENERAL_DISCOVERABLE,
        connectable_mode=BLEDut.bt.legacy_advertiser.CONNECTION_MODE_NON_CONNECTABLE,
    )

    dut_addr = get_device_addr(BLEDut)

    # Check the Advertising Packets with the Tester, since the nature of the test
    # is heavily dependant on the environment repeat 3 times if failed
    for attempt in range(MAX_TEST_RETRIES):
        log.info("Beacon detection attempt %d/%d", attempt + 1, MAX_TEST_RETRIES)
        event = bgapi_check_event(BLETester,
                                  "scanner",
                                  "legacy_advertisement_report",
                                  lambda evt: (evt.address == dut_addr))
    
        if event is None and attempt == MAX_TEST_RETRIES - 1:
            pytest.fail("Test retry maximum reached. Test failed.")
        else:
            break


# TODO - Connectable Advertising - Central
def test_connAdv_central(BLEDut,
                         BLETester,
                         TesterAdvStart):
    TesterAdvStart(
        discover_mode=BLETester.bt.advertiser.DISCOVERY_MODE_GENERAL_DISCOVERABLE,
        connectable_mode=BLETester.bt.legacy_advertiser.CONNECTION_MODE_CONNECTABLE,
    )

    dut_addr = get_device_addr(BLEDut)
    tester_addr = get_device_addr(BLETester)

    BLEDut.bt.connection.open(
        tester_addr,
        BLEDut.bt.gap.ADDRESS_TYPE_PUBLIC_ADDRESS,
        BLEDut.bt.gap.PHY_CODING_PHY_CODING_1M_UNCODED
    )

    dut_conn_evt = bgapi_check_event(
        BLEDut,
        "connection",
        "opened",
        lambda evt: evt.address == tester_addr,
        eventTimeout=1,
        maxTime=10,
    )
    assert dut_conn_evt.connection != 0

    tester_conn_evt = bgapi_check_event(
        BLETester,
        "connection",
        "opened",
        lambda evt: evt.address == dut_addr,
        eventTimeout=1,
        maxTime=10,
    )
    assert tester_conn_evt.connection != 0

    # Sleep for 500ms to allow the connection for the idle state
    sleep(0.5)

    BLEDut.bt.connection.close(dut_conn_evt.connection)

    conn_closed_evt = bgapi_check_event(
        BLEDut,
        "connection",
        "closed",
        lambda evt: evt.connection == dut_conn_evt.connection,
        eventTimeout=1,
        maxTime=10
    )
    assert conn_closed_evt is not None

    conn_closed_evt = bgapi_check_event(
        BLETester,
        "connection",
        "closed",
        lambda evt: evt.connection == tester_conn_evt.connection,
        eventTimeout=1,
        maxTime=10
    )
    assert conn_closed_evt is not None

# TODO - Connectable Advertising - Peripheral
def test_connAdv_peripheral(BLEDut,
                            BLETester,
                            DUTAdvStart):
    DUTAdvStart(
        discover_mode=BLEDut.bt.advertiser.DISCOVERY_MODE_GENERAL_DISCOVERABLE,
        connectable_mode=BLEDut.bt.legacy_advertiser.CONNECTION_MODE_CONNECTABLE,
    )

    dut_addr = get_device_addr(BLEDut)
    tester_addr = get_device_addr(BLETester)

    rsp = BLETester.bt.connection.open(
        dut_addr,
        BLETester.bt.gap.ADDRESS_TYPE_PUBLIC_ADDRESS,
        BLETester.bt.gap.PHY_CODING_PHY_CODING_1M_UNCODED
    )

    tester_conn_evt = bgapi_check_event(BLETester,
                                 "connection",
                                 "opened",
                                 lambda evt: evt.address == dut_addr,
                                 eventTimeout=1,
                                 maxTime=10,
                                 )
    assert tester_conn_evt.connection != 0

    dut_conn_evt = bgapi_check_event(BLEDut,
                                 "connection",
                                 "opened",
                                 lambda evt: evt.address == tester_addr,
                                 eventTimeout=1,
                                 maxTime=10,
                                 )
    assert dut_conn_evt.connection != 0

    # Sleep for 500ms to allow the connection for the idle state
    sleep(0.5)

    rsp = BLETester.bt.connection.close(tester_conn_evt.connection)

    conn_closed_evt = bgapi_check_event(
        BLETester,
        "connection",
        "closed",
        lambda evt: evt.connection == tester_conn_evt.connection,
        eventTimeout=1,
        maxTime=10
    )
    assert conn_closed_evt is not None

    conn_closed_evt = bgapi_check_event(
        BLEDut,
        "connection",
        "closed",
        lambda evt: evt.connection == dut_conn_evt.connection,
        eventTimeout=1,
        maxTime=10
    )
    assert conn_closed_evt is not None

# TODO - Scanning
def test_scanner(BLEDut):
    assert 1 == 0

# TODO - PAwR Observer
def test_PAwR_obs(BLEDut):
    assert 1 == 0

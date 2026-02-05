import pytest
import logging
from dataclasses import dataclass
from time import sleep
from bgapi.bglib import BGLib
from bgapi.serialconnector import SerialConnector

# TODO - BGAPI device interfaces in pytest so that we can use the HWMux thingy
# TODO - BGAPI stuff with pytest so that the tests can use
BGAPI_SDK_PATH = "C:\\Users\\sagonzal\\SimplicityStudio\\SDKs\\simplicity_sdk\\protocol\\bluetooth\\api\\sl_bt.xapi"
MAX_TEST_RETRIES = 3
log = logging.getLogger(__name__)

def makeBLEDev(serialPort):
    res = dev = BGLib(
        SerialConnector(serialPort),
        BGAPI_SDK_PATH)

    dev.open()
    dev.bt.system.reset(0)
    sleep(0.5)

    return dev

def checkBLEDev(dev):
    if dev is not None:
        rsp = dev.bt.system.hello()
        assert rsp is not None

@pytest.fixture
def BLEDut():
    dev = makeBLEDev("COM4")
    checkBLEDev(dev)

    yield dev

    dev.close()

@pytest.fixture
def BLETester():
    dev = makeBLEDev("COM5")
    checkBLEDev(dev)

    yield dev

    dev.close()

@pytest.fixture
def StartAdv(BLEDut):
    rsp = BLEDut.bt.advertiser.create_set()
    assert rsp


def test_bgapi(BLEDut,
               BLETester):
    log.info("Testing serial connection")
    checkBLEDev(BLEDut)
    checkBLEDev(BLETester)

from dataclasses import dataclass

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
def test_connAdv_central(BLEDut):
    assert 1 == 0

# TODO - Connectable Advertising - Peripheral

# TODO - Scanning
def test_scanner(BLEDut):
    assert 1 == 0

# TODO - PAwR Observer
def test_PAwR_obs(BLEDut):
    assert 1 == 0
import pytest
import logging
from dataclasses import dataclass
from time import sleep
from bgapi.bglib import BGLib
from bgapi.serialconnector import SerialConnector

# TODO - BGAPI device interfaces in pytest so that we can use the HWMux thingy
# TODO - BGAPI stuff with pytest so that the tests can use
BGAPI_SDK_PATH = "C:\\Users\\sagonzal\\SimplicityStudio\\SDKs\\simplicity_sdk\\protocol\\bluetooth\\api\\sl_bt.xapi"
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
        if rsp is not None:
            log.info(f"RSP: {rsp}")

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

TESTER_SCAN_CFG = ScannerConfig()

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
def TesterScanConfig(BLETester) -> ScannerConfig:
    config = ScannerConfig()

    # Configure the scanner
    rsp = BLETester.bt.scanner.set_parameters(BLETester.bt.scanner.SCAN_MODE_SCAN_MODE_PASSIVE,
                                            config.interval,
                                            config.window)
    assert rsp.result == 0 

    return config

# TODO - Beacon/Non-Connectable Advertising test
def test_beacon(BLEDut,
                DUTAdvConfig,
                BLETester,
                TesterScanConfig):
    # Configure the Advertiser
    rsp = BLEDut.bt.system.get_identity_address()
    assert rsp.result == 0
    dut_addr = rsp.address

    rsp = BLEDut.bt.legacy_advertiser.generate_data(DUTAdvConfig.set,
                                                    BLEDut.bt.advertiser.DISCOVERY_MODE_GENERAL_DISCOVERABLE)
    assert rsp.result == 0

    # Start advertising and scanning
    rsp = BLEDut.bt.legacy_advertiser.start(DUTAdvConfig.set,
                                            BLEDut.bt.legacy_advertiser.CONNECTION_MODE_NON_CONNECTABLE)
    assert rsp.result == 0

    rsp = BLETester.bt.scanner.start(BLETester.bt.scanner.SCAN_PHY_SCAN_PHY_1M,
                                     BLETester.bt.scanner.DISCOVER_MODE_DISCOVER_OBSERVATION)
    assert rsp.result == 0

    # Check the Advertising Packets with the Tester

    

# TODO - Connectable Advertising - Central
def test_connAdv_central(BLEDut):
    assert 1 == 0

# TODO - Connectable Advertising - Peripheral
def test_connAdv_peripheral(BLEDut):
    assert 1 == 0

# TODO - Scanning
def test_scanner(BLEDut):
    assert 1 == 0

# TODO - PAwR Observer
def test_PAwR_obs(BLEDut):
    assert 1 == 0
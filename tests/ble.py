import pytest
import logging
from time import sleep
from bgapi.bglib import BGLib
from bgapi.serialconnector import SerialConnector

# TODO - BGAPI device interfaces in pytest so that we can use the HWMux thingy
# TODO - BGAPI stuff with pytest so that the tests can use
BGAPI_SDK_PATH = "C:\\Users\\sagonzal\\SimplicityStudio\\SDKs\\simplicity_sdk\\protocol\\bluetooth\\api\\sl_bt.xapi"
log = logging.getLogger(__name__)

@pytest.fixture
def BLEDut():
    res = dev = BGLib(
        SerialConnector("COM4"),
        BGAPI_SDK_PATH)

    dev.open()
    dev.bt.system.reset(0)
    sleep(0.5)

    checkBLEDev(dev)

    yield dev
    dev.close()

@pytest.fixture
def BLETester():
    res = dev = BGLib(
        SerialConnector("COM5"),
        BGAPI_SDK_PATH)
    
    dev.open()
    dev.bt.system.reset(0)
    checkBLEDev(dev)
    yield dev
    dev.close()

@pytest.fixture
def StartAdv(BLEDut):
    rsp = BLEDut.bt.advertiser.create_set()
    assert rsp

def checkBLEDev(dev):
    if dev is not None:
        rsp = dev.bt.system.hello()
        if rsp is not None:
            log.info(f"RSP: {rsp}")

    assert rsp is not None

def test_bgapi(BLEDut,
               BLETester):
    log.info("Testing serial connection")
    checkBLEDev(BLEDut)
    checkBLEDev(BLETester)

advertiser_configuration = {
    "set" : 0,
    "min_interval" : 100,
    "max_interval" : 250,
}

def advertiser_configure(dev,
                         advertiser_configuration):
    log.info("Configuring DUT Advertiser")
    rsp = dev.bt.advertiser.create_set()
    advertiser_configuration["set"] = rsp.handle
    assert rsp.result == 0

    rsp = dev.bt.advertiser.set_timing(advertiser_configuration["set"],
                                       advertiser_configuration["min_interval"],
                                       advertiser_configuration["max_interval"],
                                       0,
                                       0)
    assert rsp.result == 0
    log.info("DUT Advertiser configured")

# TODO - Beacon/Non-Connectable Advertising test
def test_beacon(BLEDut):
    advertiser_configure(BLEDut,
                         advertiser_configuration)

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
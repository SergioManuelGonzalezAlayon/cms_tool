import pytest
import logging
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

    if dev.open() is None:
        yield dev
        
    dev.close()

@pytest.fixture
def StartAdv(BLEDut):
    rsp = BLEDut.bt.advertiser.create_set()
    assert rsp

def test_bgapi(BLEDut):
    log.info("Testing serial connection")
    rsp = BLEDut.bt.system.hello()

    if rsp is not None:
        log.info(f"RSP: {rsp}")

    assert rsp is not None

# TODO - Beacon/Non-Connectable Advertising test
def test_beacon(BLEDut):
    assert 1 == 0

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
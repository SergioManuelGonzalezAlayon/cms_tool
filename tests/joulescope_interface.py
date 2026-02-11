import pytest, logging
from dataclasses import dataclass, asdict
from time import sleep
from current_measurement import Joulescope

log = logging.getLogger(__name__)

@dataclass
class JoulescopeConfig:
    signals: str = "i,r,0,1"
    duration: str | float = 1
    frequency: int = 1_000_000
    serial_number: str | None = None
    open_mode: str = "defaults"
    vref: str = "3v3"
    post_process: str = "profile"
    irange: str = "auto"
    verbose: bool = False

@pytest.fixture(autouse=True)
def JoulescopeDev():
    log.info("Enabling Joulescope")
    cfg = JoulescopeConfig(open_mode="restore")
    js = Joulescope(**asdict(cfg))

    js.create_driver()
    js._open_devices(js.driver)

    yield js

    js.close_driver()

def test_capture(JoulescopeDev):
    """
    Test the Joulescope basic capture 
    """
    #JouleIscopeDev.run()
    pass

def test_GPIOInCheck():
    NotImplementedError

def test_GPIOOutCheck():
    NotImplementedError

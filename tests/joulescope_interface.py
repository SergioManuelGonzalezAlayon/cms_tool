import pytest, logging
from dataclasses import dataclass, asdict
from time import sleep
from current_measurement import Joulescope
from current_measurement.read_pyjoulescope_driver import get_profile
import matplotlib.pyplot as plt
from pathlib import Path

log = logging.getLogger(__name__)

def savePlotData(profile, data_type, title, ylabel, out_dir):
    filename = "result_" + title.replace(" ", "").lower() + ".png"

    plt.figure()
    plt.plot(profile[data_type]["timestamp"],
             profile[data_type]["samples"])
    plt.title(title)
    plt.xlabel("Time (s)")
    plt.ylabel(ylabel)
    plt.savefig(out_dir / filename , dpi=149, bbox_inches="tight")
    plt.close()

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

@pytest.fixture(scope="session")
def JoulescopeDev():
    log.info("Enabling Joulescope")
    cfg = JoulescopeConfig(open_mode="restore")
    js = Joulescope(**asdict(cfg))

    js.create_driver()
    js.open_devices()

    yield js

    # Save the capture data
    if getattr(js, "capture_data", None) is not None:
        log.info("Saving captured plots")
        profile = get_profile(js, js.capture_data)

        out_dir = Path("artifacts")
        out_dir.mkdir(exist_ok=True)

        if "current" in profile:
            savePlotData(profile, "current", "Current", "mA", out_dir)

        if "gpio[0]" in profile:
            savePlotData(profile, "gpio[0]", "GPIO 0", "Level", out_dir)

        if "gpio[1]" in profile:
            savePlotData(profile, "gpio[1]", "GPIO 1", "Level", out_dir)
    
        log.info("Plots saved")

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

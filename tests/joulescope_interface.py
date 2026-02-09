import pytest, logging
import serial.tools.list_ports
from current_measurement import read_pyjoulescope_driver

log = logging.getLogger(__name__)

def test_connection():
    js = read_pyjoulescope_driver.Joulescope(duration='1s', frequency=1000000, signals='i,r,0,1', post_process='profile')
    js.run()

def test_GPIOInCheck():
    NotImplementedError

def test_GPIOOutCheck():
    NotImplementedError
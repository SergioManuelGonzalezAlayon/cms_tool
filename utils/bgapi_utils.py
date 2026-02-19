import json
import logging
import os
from dataclasses import dataclass
from time import sleep
from bgapi.bglib import BGLib
from bgapi.serialconnector import SerialConnector

log = logging.getLogger(__name__)

class BGAPIEventTimeoutError(RuntimeError):
    def __init__(self,
                 event_class,
                 event_name,
                 max_time,
                 event_timeout,
                 seen_events=None):
        self.event_class = event_class
        self.event_name = event_name
        self.max_time = max_time
        self.event_timeout = event_timeout
        self.seen_events = seen_events or []

        message = (
            f"BGAPI event '{event_class}.{event_name}' not received.\n"
            f"Waited max_time={max_time}s (poll timeout={event_timeout}s).\n"
            f"Seen events during wait: {self._format_seen()}"
        )
        super().__init__(message)

    def _format_seen(self):
        if not self.seen_events:
            return "None"
        return ", ".join(
            f"{evt._class_name}.{evt._msg_name}"
            for evt in self.seen_events
        )

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


def read_dev_config(section, key=None, default=None, *, path="dev_config.json"):
    config_path = os.path.join(os.getcwd(), path)
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if key is None:
        return data.get(section, default)

    return data.get(section, {}).get(key, default)


def get_ble_dev(serialPort):
    try:
        dev = BGLib(
            SerialConnector(serialPort),
            read_dev_config("ble", "xapi"))

        return dev
    except Exception as e:
        log.error(f"Cannot create the BGAPI interface with the device. Reason : {e}")
        raise

def open_dev(dev):
    try:
        dev.open()
        dev.bt.system.reset(0)
        sleep(0.5)
    except Exception:
        log.exception("Failed to open/reset BGAPI device")
        raise

def bgapi_check_event(dev,
                      event_class,
                      event_name,
                      condition,
                      eventTimeout=0.2,
                      maxTime=5,
                      debug=False):
    """
    Check a BGAPI event and return the event itself if there is
    any processing required for it.

    :dev: BGAPI Dev
    :event_class: BGAPI Class ID as a string
    :event_name: BGAPI Event ID as a string
    :condition: Condition to find the event
    :eventTimeout: BGAPI Event timeout until other event is popped from the dev queue
    :maxTime: Maximum time to wait for that BGAPI Event on the dev queue
    """
    seen = []

    for evt in dev.get_events(timeout=eventTimeout, max_time=maxTime):
        seen.append(evt)
        if debug:
            log.info(f"> DEBUG BGAPI Evt > {evt}\n")
        if evt._class_name == event_class and evt._msg_name == event_name:
            if condition(evt):
                log.info(f"{event_class}_{event_name} found")
                return evt

    raise BGAPIEventTimeoutError(
        event_class=event_class,
        event_name=event_name,
        max_time=maxTime,
        event_timeout=eventTimeout,
        seen_events=seen
    )

def get_device_addr(dev):
    rsp = dev.bt.system.get_identity_address()
    assert rsp.result == 0
    return rsp.address
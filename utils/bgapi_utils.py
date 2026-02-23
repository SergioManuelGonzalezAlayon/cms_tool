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
    max_interval : int = 100
    latency : int = 0
    timeout : int = 0
    min_ce_lenght : int = 0
    max_ce_lenght : int = 0xffff


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

def is_conn_handle_valid(handle):
    return (handle != 255 and handle > 0)

def close_connection(dev, conn_handle):
    try:
        if is_conn_handle_valid(conn_handle):
            log.info("Closing partial central connection conn_handle %s", conn_handle)
            dev.bt.connection.close(conn_handle)

    except Exception as ex:
        log.debug("Error while closing central partial connection: %s", ex)

def connection_set_params(dev, params):
    dev.bt.connection.set_default_parameters(
    params.min_interval,
    params.max_interval,
    params.latency,
    params.timeout,
    params.min_ce_lenght,
    params.max_ce_lenght)

def connect_devices(central, 
                    peripheral,
                    phy,
                    parameters : ConnectionParameters,
                    retry_sleep=0.25,
                    max_retries=3):
    """
    Connect two BLE devices where the peripheral has started Connectable Advertising,
    with a maximum number of retries
    """
    central_conn = -1
    peripheral_conn = -1

    rsp = peripheral.bt.system.get_identity_address()
    peripheral_addr = rsp.address
    peripheral_addr_type = rsp.type

    central_addr = central.bt.system.get_identity_address().address
    
    # Configure the connection parameters
    connection_set_params(central, parameters)
    connection_set_params(peripheral, parameters)

    # Connection attempts
    for attempt in range(max_retries):
        log.info("Connection attempt %d/%d", attempt + 1, max_retries)

        rsp = central.bt.connection.open(
            peripheral_addr,
            peripheral_addr_type,
            phy,
        )
        assert rsp.result == 0

        try:
            central_event = bgapi_check_event(
                central,
                "connection",
                "opened",
                lambda evt: evt.address == peripheral_addr,
                eventTimeout=1,
                maxTime=5,
            )
            
            peripheral_event = bgapi_check_event(
                peripheral,
                "connection",
                "opened",
                lambda evt: evt.address == central_addr,
                eventTimeout=1,
                maxTime=5,
            )

            central_conn = central_event.connection
            peripheral_conn = peripheral_event.connection

            if is_conn_handle_valid(central_conn) and is_conn_handle_valid(peripheral_conn):
                log.info("Devices connected")
                return central_conn, peripheral_conn

        except BGAPIEventTimeoutError as be:
            log.warning("Connection attempt %d/%d failed", attempt + 1, max_retries)

            close_connection(central, central_conn)
            close_connection(peripheral, peripheral_conn)

            sleep(retry_sleep)
            continue


    # All attempts exhausted
    raise ConnectionError(
        f"Could not establish connection between central ({getattr(central, 'name', repr(central))}) "
        f"and peripheral ({getattr(peripheral, 'name', repr(peripheral))}) after {max_retries} attempts"
    )

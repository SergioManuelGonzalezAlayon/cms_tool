import json
import logging
import os
import secrets
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from time import sleep

from bgapi.bglib import BGLib
from bgapi.serialconnector import SerialConnector

log = logging.getLogger(__name__)

ADV_INTERVAL_UNIT_MS = Decimal("0.625")
CONN_INTERVAL_UNIT_MS = Decimal("1.25")
ADV_DURATION_UNIT_MS = Decimal("10")
ADV_INTERVAL_MIN_UNITS = 0x20
ADV_INTERVAL_MAX_UNITS = 0xFFFFFF
CONN_INTERVAL_MIN_UNITS = 0x0006
CONN_INTERVAL_MAX_UNITS = 0x0C80


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
    max_interval: int = 200
    interval: int | None = None
    payload: int = 20
    phy: str | int = "1mbps"
    tx_power: int | float = 0
    duration: int = 0
    max_events: int = 0


@dataclass
class ScannerConfig:
    window: int = 50
    interval: int = 100


@dataclass
class ConnectionParameters:
    min_interval: int | None = None
    max_interval: int | None = None
    interval: int | None = 15
    latency: int = 0
    timeout: int = 100
    min_ce_lenght: int = 0
    max_ce_lenght: int = 0xffff
    rx_payload_length :int = 0
    tx_payload_length :int = 0


@dataclass
class PAWRConfig:
    num_subevents: int = 0
    subevent_interval: int = 0
    response_slot_delay: int = 0
    response_slot_spacing: int = 0
    response_slots: int = 0


def _decimal_ms(value) -> Decimal:
    return Decimal(str(value))


def bgapi_units_from_ms(milliseconds, unit_ms: Decimal) -> int:
    units = (_decimal_ms(milliseconds) / unit_ms).to_integral_value(rounding=ROUND_HALF_UP)
    actual_ms = Decimal(units) * unit_ms
    requested_ms = _decimal_ms(milliseconds)
    if actual_ms != requested_ms:
        log.warning(
            "Requested %sms cannot be represented exactly with %sms BGAPI units. Using %sms instead.",
            requested_ms,
            unit_ms,
            actual_ms,
        )
    return int(units)


def ms_from_bgapi_units(units: int, unit_ms: Decimal) -> Decimal:
    return Decimal(units) * unit_ms


def advertising_units_from_ms(milliseconds) -> int:
    return bgapi_units_from_ms(milliseconds, ADV_INTERVAL_UNIT_MS)


def advertising_ms_from_units(units: int) -> Decimal:
    return ms_from_bgapi_units(units, ADV_INTERVAL_UNIT_MS)


def connection_units_from_ms(milliseconds) -> int:
    return bgapi_units_from_ms(milliseconds, CONN_INTERVAL_UNIT_MS)


def connection_ms_from_units(units: int) -> Decimal:
    return ms_from_bgapi_units(units, CONN_INTERVAL_UNIT_MS)


def advertising_duration_units_from_ms(milliseconds: int | float | Decimal) -> int:
    return int((_decimal_ms(milliseconds) / ADV_DURATION_UNIT_MS).to_integral_value(rounding=ROUND_HALF_UP))


def advertising_tx_power_from_dbm(value: int | float | Decimal) -> int:
    return int((Decimal(str(value)) * Decimal("10")).to_integral_value(rounding=ROUND_HALF_UP))


def advertising_phy_from_str(value: str | int) -> int:
    if isinstance(value, int):
        return value

    normalized = value.strip().lower().replace(" ", "")
    phy_map = {
        "1mbps": 0x1,
        "1000kbps": 0x1,
        "1m": 0x1,
        "2mbps": 0x2,
        "2000kbps": 0x2,
        "2m": 0x2,
        "125kbps": 0x4,
        "500kbps": 0x4,
        "coded": 0x4,
        "longrange": 0x4,
        "any": 0xFF,
    }
    try:
        return phy_map[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported advertiser phy: {value!r}") from exc


def random_payload_bytes(payload_length: int) -> bytes:
    if payload_length < 0:
        raise ValueError(f"payload_length must be >= 0, got {payload_length}")
    return secrets.token_bytes(payload_length)


def normalize_advertiser_config(config: AdvertiserConfig) -> AdvertiserConfig:
    interval_ms = config.interval
    min_interval_ms = interval_ms if interval_ms is not None else config.min_interval
    max_interval_ms = interval_ms if interval_ms is not None else config.max_interval
    config.min_interval = advertising_units_from_ms(min_interval_ms)
    config.max_interval = advertising_units_from_ms(max_interval_ms)
    if config.min_interval < ADV_INTERVAL_MIN_UNITS:
        raise ValueError(f"Advertising min_interval must be >= 20 ms, got {min_interval_ms!r} ms")
    if config.max_interval < ADV_INTERVAL_MIN_UNITS:
        raise ValueError(f"Advertising max_interval must be >= 20 ms, got {max_interval_ms!r} ms")
    if config.min_interval > ADV_INTERVAL_MAX_UNITS or config.max_interval > ADV_INTERVAL_MAX_UNITS:
        raise ValueError("Advertising interval exceeds BGAPI range")
    if config.max_interval < config.min_interval:
        raise ValueError(
            f"Advertising max_interval must be >= min_interval, got {max_interval_ms!r} ms < {min_interval_ms!r} ms"
        )
    config.phy = advertising_phy_from_str(config.phy)
    config.tx_power = advertising_tx_power_from_dbm(config.tx_power)
    if config.duration < 0:
        raise ValueError(f"Advertising duration must be >= 0, got {config.duration}")
    if config.duration > 0:
        config.duration = advertising_duration_units_from_ms(config.duration)
    config.payload = random_payload_bytes(config.payload)
    return config


def normalize_scanner_config(config: ScannerConfig) -> ScannerConfig:
    config.interval = advertising_units_from_ms(config.interval)
    config.window = advertising_units_from_ms(config.window)
    return config


def normalize_connection_parameters(params: ConnectionParameters) -> ConnectionParameters:
    interval_ms = params.interval
    min_interval_ms = interval_ms if interval_ms is not None else params.min_interval
    max_interval_ms = interval_ms if interval_ms is not None else params.max_interval
    params.min_interval = connection_units_from_ms(min_interval_ms)
    params.max_interval = connection_units_from_ms(max_interval_ms)
    if params.min_interval < CONN_INTERVAL_MIN_UNITS:
        raise ValueError(f"Connection min_interval must be >= 7.5 ms, got {min_interval_ms!r} ms")
    if params.max_interval < CONN_INTERVAL_MIN_UNITS:
        raise ValueError(f"Connection max_interval must be >= 7.5 ms, got {max_interval_ms!r} ms")
    if params.min_interval > CONN_INTERVAL_MAX_UNITS or params.max_interval > CONN_INTERVAL_MAX_UNITS:
        raise ValueError("Connection interval exceeds BGAPI range")
    if params.max_interval < params.min_interval:
        raise ValueError(
            f"Connection max_interval must be >= min_interval, got {max_interval_ms!r} ms < {min_interval_ms!r} ms"
        )
    if params.timeout <= 0:
        log.warning("Connection supervision timeout %s is invalid. Using 100 (1.0 s).", params.timeout)
        params.timeout = 100
    return params


def read_dev_config(section, key=None, default=None, *, path="dev_config.json"):
    config_path = os.path.join(os.getcwd(), path)
    with open(config_path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    if key is None:
        return data.get(section, default)

    return data.get(section, {}).get(key, default)


def get_ble_dev(serialPort):
    try:
        xapi_path = read_dev_config("ble", "xapi") or os.getenv("BGAPI_XAPI")
        if not xapi_path:
            raise RuntimeError(
                "Missing BGAPI XAPI path. Set dev_config.json 'ble.xapi' or environment variable BGAPI_XAPI."
            )

        dev = BGLib(
            SerialConnector(serialPort),
            xapi_path)

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
                    parameters: ConnectionParameters,
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

    connection_set_params(central, parameters)
    connection_set_params(peripheral, parameters)

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

        except BGAPIEventTimeoutError:
            log.warning("Connection attempt %d/%d failed", attempt + 1, max_retries)

            close_connection(central, central_conn)
            close_connection(peripheral, peripheral_conn)

            sleep(retry_sleep)
            continue

    raise ConnectionError(
        f"Could not establish connection between central ({getattr(central, 'name', repr(central))}) "
        f"and peripheral ({getattr(peripheral, 'name', repr(peripheral))}) after {max_retries} attempts"
    )

import pytest
from types import SimpleNamespace

from utils.bgapi_utils import (
    PHY_1M,
    PHY_1M_AND_CODED,
    PHY_2M,
    PHY_CODED,
    ConnectionParameters,
    PAWRConfig,
    connect_devices,
    connection_open_phy,
    discovery_scan_phy_for_advertiser,
    is_conn_handle_valid,
    normalize_connection_parameters,
    pawr_interval_units_from_ms,
    start_pawr_advertising,
    sync_connection_config_actuals,
)


def test_connection_open_phy_uses_1m_for_2m_connections() -> None:
    assert connection_open_phy("2m") == PHY_1M
    assert connection_open_phy(PHY_2M) == PHY_1M


def test_connection_open_phy_keeps_discovery_capable_phys() -> None:
    assert connection_open_phy("1m") == PHY_1M
    assert connection_open_phy("coded") == PHY_CODED


def test_discovery_scan_phy_for_coded_advertising_scans_1m_and_coded() -> None:
    assert discovery_scan_phy_for_advertiser("1m") == PHY_1M
    assert discovery_scan_phy_for_advertiser("coded") == PHY_1M_AND_CODED


def test_connection_handle_zero_is_valid() -> None:
    assert is_conn_handle_valid(0)
    assert is_conn_handle_valid(1)
    assert not is_conn_handle_valid(255)
    assert not is_conn_handle_valid(None)


def test_normalize_connection_parameters_keeps_tx_power_in_dbm_units() -> None:
    params = normalize_connection_parameters(
        ConnectionParameters(interval=15, timeout=100, tx_power=20, phy="1m")
    )

    assert params.tx_power == 20


def test_normalize_connection_parameters_requires_timeout_above_supervision_limit() -> None:
    with pytest.raises(ValueError, match="supervision timeout is too small"):
        normalize_connection_parameters(
            ConnectionParameters(interval=100, latency=0, timeout=20, phy="1m")
        )

    params = normalize_connection_parameters(
        ConnectionParameters(interval=100, latency=0, timeout=21, phy="1m")
    )
    assert params.timeout == 21


def test_pawr_interval_uses_1_25_ms_periodic_advertising_units() -> None:
    assert pawr_interval_units_from_ms(1000) == 800


def test_pawr_interval_rejects_values_outside_bgapi_range() -> None:
    with pytest.raises(ValueError, match="PAwR interval must be >= 7.5 ms"):
        pawr_interval_units_from_ms(5)

    with pytest.raises(ValueError, match="PAwR interval exceeds BGAPI range"):
        pawr_interval_units_from_ms(81920)


def test_start_pawr_advertising_uses_configured_interval_for_both_bounds() -> None:
    class _PawrAdvertiserApi:
        def __init__(self):
            self.args = None

        def start(self, *args):
            self.args = args
            return SimpleNamespace(result=0)

    pawr_api = _PawrAdvertiserApi()
    device = SimpleNamespace(bt=SimpleNamespace(pawr_advertiser=pawr_api))
    config = PAWRConfig(
        interval=1000,
        num_subevents=2,
        subevent_interval=6,
        response_slot_delay=1,
        response_slot_spacing=3,
        response_slots=4,
    )

    response = start_pawr_advertising(device, 7, config, flags=0x10)

    assert response.result == 0
    assert pawr_api.args == (7, 800, 800, 0x10, 2, 6, 1, 3, 4)


def test_sync_connection_config_actuals_uses_representable_interval() -> None:
    config = {"connection": {"interval": 1006}}
    params = normalize_connection_parameters(
        ConnectionParameters(interval=1006, timeout=300, phy="1m")
    )

    sync_connection_config_actuals(config, params)

    assert config["connection"]["interval"] == 1006.25


class _Response:
    def __init__(self, **kwargs):
        self.result = 0
        self.__dict__.update(kwargs)


class _Event:
    def __init__(self, **kwargs):
        self._class_name = "connection"
        self._msg_name = "opened"
        self.__dict__.update(kwargs)


class _SystemApi:
    def __init__(self, address):
        self._address = address

    def get_identity_address(self):
        return _Response(address=self._address, type=0)


class _GattApi:
    def set_max_mtu(self, _mtu):
        return _Response()


class _ConnectionApi:
    def __init__(self, device):
        self._device = device

    def set_default_parameters(self, *_args):
        return _Response()

    def set_default_preferred_phy(self, *_args):
        return _Response()

    def open(self, *_args):
        return _Response()

    def close(self, connection):
        self._device.closed_connections.append(connection)
        return _Response()


class _BtApi:
    def __init__(self, device, address):
        self.system = _SystemApi(address)
        self.connection = _ConnectionApi(device)
        self.gatt = _GattApi()


class _Device:
    def __init__(self, address, event_batches):
        self.closed_connections = []
        self._event_batches = list(event_batches)
        self.bt = _BtApi(self, address)

    def get_events(self, **_kwargs):
        if not self._event_batches:
            return iter(())
        return iter(self._event_batches.pop(0))


def test_connect_devices_closes_central_handle_when_peripheral_open_event_times_out() -> None:
    central = _Device("central", [[_Event(address="peripheral", connection=7)]])
    peripheral = _Device("peripheral", [[]])
    params = ConnectionParameters(
        min_interval=12,
        max_interval=12,
        timeout=100,
        phy=PHY_1M,
    )

    try:
        connect_devices(
            central,
            peripheral,
            params,
            retry_sleep=0,
            max_retries=1,
            connTimeout=0.01,
            evtTimeout=0.01,
        )
    except ConnectionError:
        pass
    else:
        raise AssertionError("connect_devices should fail when peripheral event is missing")

    assert central.closed_connections == [7]
    assert peripheral.closed_connections == []

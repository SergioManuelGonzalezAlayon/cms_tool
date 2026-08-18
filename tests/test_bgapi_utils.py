from utils.bgapi_utils import (
    PHY_1M,
    PHY_1M_AND_CODED,
    PHY_2M,
    PHY_CODED,
    ConnectionParameters,
    connect_devices,
    connection_open_phy,
    discovery_scan_phy_for_advertiser,
    is_conn_handle_valid,
    normalize_connection_parameters,
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

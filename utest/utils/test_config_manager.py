import json
import importlib.util
from pathlib import Path
from decimal import Decimal


CONFIG_MANAGER_PATH = Path(__file__).resolve().parents[2] / "tests" / "configs" / "config_manager.py"
BLE_CONFIGS_PATH = Path(__file__).resolve().parents[2] / "tests" / "configs" / "ble"
SPEC = importlib.util.spec_from_file_location("config_manager_under_test", CONFIG_MANAGER_PATH)
config_manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config_manager)


def test_advertising_phy_expansion_makes_2m_secondary_phy_discoverable() -> None:
    config = {"advertiser": {}, "scanner": {}}
    parameter = {
        "name": "phy",
        "config_paths": [
            "advertiser.phy",
            "scanner.phy",
        ],
    }

    config_manager.apply_value(config, parameter, "2m")

    assert config["advertiser"]["phy"] == "2m"
    assert config["advertiser"]["primary_phy"] == "1m"
    assert config["advertiser"]["secondary_phy"] == "2m"
    assert config["scanner"]["phy"] == "1m"


def test_advertising_phy_expansion_preserves_connection_phy_overrides() -> None:
    config = {"advertiser": {}, "scanner": {}, "connection": {}}
    parameter = {
        "name": "phy",
        "config_paths": [
            "advertiser.phy",
            "scanner.phy",
            "connection.phy",
        ],
        "config_path_values": {
            "advertiser.phy": {
                "coded": "coded",
                "1m": "1m",
                "2m": "1m",
            },
            "scanner.phy": {
                "coded": "coded",
                "1m": "1m",
                "2m": "1m",
            },
        },
    }

    config_manager.apply_value(config, parameter, "2m")

    assert config["advertiser"] == {"phy": "1m"}
    assert config["scanner"] == {"phy": "1m"}
    assert config["connection"] == {"phy": "2m"}


def _assert_connection_timeout_is_valid(connection: dict) -> None:
    interval_units = config_manager.connection_interval_units(connection["interval"])
    interval_ms = config_manager.connection_ms_from_units(interval_units)
    timeout_ms = Decimal(connection["timeout"]) * config_manager.CONN_TIMEOUT_UNIT_MS
    minimum_timeout_ms = Decimal(2 * (1 + connection.get("latency", 0))) * interval_ms

    assert timeout_ms > minimum_timeout_ms


def test_generated_connection_timeout_strictly_exceeds_supervision_limit(tmp_path: Path) -> None:
    (tmp_path / "default.json").write_text(
        json.dumps({"connection": {"interval": 100, "latency": 4, "timeout": 10}}),
        encoding="utf-8",
    )
    (tmp_path / "parameters.json").write_text(
        json.dumps(
            {
                "generator": {
                    "base_config": "default.json",
                    "output_dir": ".",
                    "steps": 1,
                    "analyses": {"one_factor": {"enabled": True}},
                },
                "parameters": [
                    {
                        "name": "connection_interval",
                        "default": "100 ms",
                        "file_token": "interval",
                        "range": {"min": "100 ms", "max": "200 ms"},
                        "config_path": "connection.interval",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = config_manager.generate_config_matrix(tmp_path, modes={"one_factor"})

    assert result["one_factor_count"] == 1
    generated = json.loads((tmp_path / "interval_200ms.json").read_text(encoding="utf-8"))
    assert generated["connection"]["timeout"] == 201
    _assert_connection_timeout_is_valid(generated["connection"])


def test_checked_in_connection_defaults_obey_supervision_timeout_rule() -> None:
    for suite in ("conn_central", "conn_peripheral"):
        config = json.loads(
            (BLE_CONFIGS_PATH / suite / "default.json").read_text(encoding="utf-8")
        )
        _assert_connection_timeout_is_valid(config["connection"])


def _pawr_config(*, response_slot_spacing: int = 3, num_subevents: int = 1,
                 subevent_interval: int = 6, interval: int = 1000) -> dict:
    return {
        "advertiser": {"payload": 10, "phy": "1m"},
        "pawr": {
            "interval": interval,
            "rx_payload_length": 10,
            "num_subevents": num_subevents,
            "subevent_interval": subevent_interval,
            "response_slot_delay": 1,
            "response_slot_spacing": response_slot_spacing,
            "response_slots": 1,
        },
    }


def test_pawr_generator_validates_response_slot_spacing_in_bgapi_units() -> None:
    assert not config_manager.is_generated_pawr_config_valid(
        _pawr_config(response_slot_spacing=2)
    )
    assert config_manager.is_generated_pawr_config_valid(
        _pawr_config(response_slot_spacing=3)
    )


def test_pawr_generator_rejects_subevents_that_exceed_periodic_interval() -> None:
    config = _pawr_config(num_subevents=4, subevent_interval=255)

    assert not config_manager.is_generated_pawr_config_valid(config)


def test_checked_in_pawr_defaults_pass_timing_validation() -> None:
    for suite in ("PAwR_obs", "PAwR_per"):
        config = json.loads(
            (BLE_CONFIGS_PATH / suite / "default.json").read_text(encoding="utf-8")
        )

        assert config_manager.is_generated_pawr_config_valid(config)

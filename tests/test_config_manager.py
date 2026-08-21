import importlib.util
from pathlib import Path


CONFIG_MANAGER_PATH = Path(__file__).parent / "configs" / "config_manager.py"
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

from pathlib import Path

import pytest

from utils.config_loader import load_test_config, validate_test_config_data


BLE_CONFIG_ROOT = Path(__file__).resolve().parents[1] / "tests" / "configs" / "ble"
BLE_FEATURE_DIRS = tuple(sorted(path for path in BLE_CONFIG_ROOT.iterdir() if path.is_dir()))


@pytest.mark.parametrize("feature_dir", BLE_FEATURE_DIRS, ids=lambda path: path.name)
def test_default_ble_config_is_valid(feature_dir):
    config_path = feature_dir / "default.json"
    assert config_path.is_file(), f"Missing default config: {config_path}"

    config = load_test_config(config_path)
    validate_test_config_data(config)


@pytest.mark.parametrize("feature", ("PAwR_obs", "PAwR_per"))
def test_pawr_default_uses_no_reporting(feature):
    config_path = BLE_CONFIG_ROOT / feature / "default.json"
    config = load_test_config(config_path)

    assert config["sync"]["reporting_mode"] == "NONE"

import json
from pathlib import Path

import pytest

from cms_protocol import VariableFilter
from cms_variable_filters import (
    config_matches_variable_filters,
    parse_variable_filter,
    validate_variable_filters,
    variable_help_text,
    variable_options_from_parameters,
)


def _parameters(path: Path) -> Path:
    parameters_path = path / "parameters.json"
    parameters_path.write_text(
        json.dumps(
            {
                "parameters": [
                    {
                        "name": "phy",
                        "values": ["coded", "1m", "2m"],
                        "config_paths": ["advertiser.phy", "scanner.phy"],
                    },
                    {
                        "name": "supply_voltage",
                        "full_sweep_values": {
                            "dcdc": ["2.4 V", "3.3 V"],
                            "no_dcdc": ["1.8 V", "2.4 V"],
                        },
                        "config_path": "hw.supply_v",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return parameters_path


def test_parse_variable_filter_uses_name_value_contract() -> None:
    assert parse_variable_filter(" phy = 2m ") == VariableFilter(name="phy", value="2m")


def test_parse_variable_filter_rejects_missing_separator() -> None:
    with pytest.raises(ValueError, match="name=value"):
        parse_variable_filter("phy")


def test_variable_options_are_discovered_from_parameters_json(tmp_path: Path) -> None:
    parameters_path = _parameters(tmp_path)

    assert variable_options_from_parameters(parameters_path) == {
        "phy": ("coded", "1m", "2m"),
        "supply_voltage": ("2.4 V", "3.3 V", "1.8 V"),
    }


def test_validate_variable_filters_rejects_unknown_name_from_metadata(tmp_path: Path) -> None:
    parameters_path = _parameters(tmp_path)

    with pytest.raises(ValueError, match="unknown variable 'missing'"):
        validate_variable_filters(parameters_path, (VariableFilter("missing", "2m"),))


def test_validate_variable_filters_rejects_unsupported_value_from_metadata(tmp_path: Path) -> None:
    parameters_path = _parameters(tmp_path)

    with pytest.raises(ValueError, match="unsupported value '3m'"):
        validate_variable_filters(parameters_path, (VariableFilter("phy", "3m"),))


def test_variable_help_text_uses_parameters_json_values(tmp_path: Path) -> None:
    parameters_path = _parameters(tmp_path)

    text = variable_help_text(parameters_path)

    assert "phy: coded, 1m, 2m" in text
    assert "supply_voltage: 2.4 V, 3.3 V, 1.8 V" in text


def test_config_matching_uses_declared_config_paths(tmp_path: Path) -> None:
    parameters_path = _parameters(tmp_path)

    assert config_matches_variable_filters(
        parameters_path,
        {"advertiser": {"phy": "2m"}, "scanner": {"phy": "1m"}},
        (VariableFilter("phy", "2m"),),
    )
    assert not config_matches_variable_filters(
        parameters_path,
        {"advertiser": {"phy": "coded"}, "scanner": {"phy": "coded"}},
        (VariableFilter("phy", "2m"),),
    )


def test_config_matching_uses_generator_runtime_values_for_ranges(tmp_path: Path) -> None:
    parameters_path = tmp_path / "parameters.json"
    parameters_path.write_text(
        json.dumps(
            {
                "generator": {"steps": 4},
                "parameters": [
                    {
                        "name": "tx_power",
                        "default": "0 dBm",
                        "range": {"min": "0 dBm", "max": "20 dBm"},
                        "config_path": "advertiser.tx_power",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert config_matches_variable_filters(
        parameters_path,
        {"advertiser": {"tx_power": 10}},
        (VariableFilter("tx_power", "10dbm"),),
    )

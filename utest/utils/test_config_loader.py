import json
from pathlib import Path

import pytest

from utils.config_loader import (
    effective_dev_config,
    load_test_config,
    validate_ble_test_config,
)


def test_load_test_config_rejects_non_object_and_non_object_sections(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid.json"
    config_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_test_config(config_path)

    config_path.write_text('{"advertiser": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="section must be an object: advertiser"):
        load_test_config(config_path)


def test_validate_ble_test_config_uses_bgapi_parameter_validation() -> None:
    validate_ble_test_config({"advertiser": {"interval": 100, "phy": "1m"}})

    with pytest.raises(ValueError, match="invalid advertiser BLE parameters"):
        validate_ble_test_config({"advertiser": {"interval": 10, "phy": "1m"}})


def test_effective_dev_config_accepts_pytest_string_paths(tmp_path: Path) -> None:
    runtime_path = tmp_path / "cms_bench_runtime.json"
    metadata_path = tmp_path / "cms_fw_metadata.json"
    runtime_path.write_text(
        json.dumps(
            {
                "bench": {
                    "bench_id": "cms-bench-001",
                },
                "devices": {
                    "board": "brd4182a",
                    "device": "EFR32MG22C224F512IM40",
                },
                "tools": {
                    "commander": "/opt/silabs/commander",
                    "jlink_library": "/opt/silabs/libjlinkarm.so",
                },
                "ble": {
                    "sdk": {
                        "name": "com.silabs.sdk.stack.sisdk",
                        "source": "client_config_package_path",
                        "version": "2025.12.3",
                        "commit": "sdk-commit",
                        "hash": "sdk-hash",
                        "xapi_sha256": "xapi-hash",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    metadata_path.write_text(
        json.dumps(
            {
                "firmware": {"sisdk": "2025.12.3"},
                "vcom": {
                    "enabled": True,
                    "gpio": {
                        "dout_address": "0x5003C040",
                        "pin_mask": "0x00000010",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    config = effective_dev_config(
        runtime_path=str(runtime_path),
        metadata_path=str(metadata_path),
    )

    assert config["tools"]["commander"] == "/opt/silabs/commander"
    assert config["tools"]["jlink_library"] == "/opt/silabs/libjlinkarm.so"
    assert config["firmware"]["board"] == "brd4182a"
    assert config["bench"]["bench_id"] == "cms-bench-001"
    assert config["firmware"]["sisdk"] == "2025.12.3"
    assert config["firmware"]["sisdk_name"] == "com.silabs.sdk.stack.sisdk"
    assert config["firmware"]["sisdk_version"] == "2025.12.3"
    assert config["firmware"]["xapi_file_hash"] == "xapi-hash"
    assert config["vcom"]["gpio"]["dout_address"] == "0x5003C040"

import json
from pathlib import Path

from utils.config_loader import effective_dev_config


def test_effective_dev_config_accepts_pytest_string_paths(tmp_path: Path) -> None:
    runtime_path = tmp_path / "cms_bench_runtime.json"
    metadata_path = tmp_path / "cms_fw_metadata.json"
    runtime_path.write_text(
        json.dumps(
            {
                "devices": {
                    "board": "brd4182a",
                    "device": "EFR32MG22C224F512IM40",
                },
                "tools": {"commander": "/opt/silabs/commander"},
            }
        ),
        encoding="utf-8",
    )
    metadata_path.write_text(
        json.dumps({"firmware": {"sisdk": "2025.12.3"}}),
        encoding="utf-8",
    )

    config = effective_dev_config(
        runtime_path=str(runtime_path),
        metadata_path=str(metadata_path),
    )

    assert config["tools"]["commander"] == "/opt/silabs/commander"
    assert config["firmware"]["board"] == "brd4182a"
    assert config["firmware"]["sisdk"] == "2025.12.3"

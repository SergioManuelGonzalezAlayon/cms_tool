from pathlib import Path

from cms_client import load_client_config


CONFIG_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "configs" / "cms_client_config.json"


def test_reference_client_config_loads_expected_endpoints_and_settings():
    config = load_client_config(CONFIG_PATH)

    assert config.base_url == "http://127.0.0.1:8000"
    assert config.api_token == "dummy-daemon-token-not-for-use"
    assert config.timeout_s == 12.0
    assert config.benches["cms-bench-dummy-001"].base_url == "http://127.0.0.1:8001"
    assert config.benches["cms-bench-dummy-001"].api_token == "dummy-bench-token-not-for-use"
    assert config.tools["commander"] == "tools/commander"
    assert config.build["bootloader_slcp"] == "apps/bootloader-uart-bgapi.slcp"
    assert config.target["board"] == "BRD4182A"
    assert config.diff["sisdk_version2_path"] == "sdk/child"

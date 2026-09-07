from pathlib import Path

from conftest import _firmware_project_name


def test_configured_dut_slcp_selects_the_matching_dcdc_or_no_dcdc_project() -> None:
    assert _firmware_project_name(Path("apps/ncp_ble_dut_dcdc.slcp")) == "ncp_ble_dut_dcdc"
    assert _firmware_project_name(Path("apps/ncp_ble_dut.slcp")) == "ncp_ble_dut"

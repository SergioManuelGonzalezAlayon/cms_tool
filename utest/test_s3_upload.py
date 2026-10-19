import json

import pytest

import s3_upload


pytestmark = pytest.mark.skip(reason="S3 upload tests are run separately")


def test_record_upload_location_uses_cms_storage_metadata(tmp_path):
    result_path = tmp_path / "conn_central_default_result.json"
    result_path.write_text(
        json.dumps(
            {
                "storage": {
                    "cms_bench_id": "Timmothy",
                    "protocol": "BLE",
                    "test_case": "conn_central",
                    "partition": "no_dcdc",
                    "part_number": "EFR32MG22C224F512IM40",
                    "board": "BRD4182C",
                    "board_revision": "A06",
                    "serial_number": "212300218",
                    "test_name": "conn_central",
                    "config_name": "default",
                },
                "test_run": {
                    "test_name": "conn_central",
                    "config_name": "default",
                    "config_path": "tests/configs/ble/conn_central/full_sweep/no_dcdc/default.json",
                },
            }
        ),
        encoding="utf-8",
    )

    file_name, partition = s3_upload._record_upload_location(result_path)

    assert file_name == "default.json"
    assert partition == "cms/timmothy/efr32mg22c224f512im40/brd4182c_a06/212300218/ble/no_dcdc/conn_central"


def test_record_upload_location_keeps_empty_serial_path_component(tmp_path):
    result_path = tmp_path / "beacon_default_result.json"
    result_path.write_text(
        json.dumps(
            {
                "dataset": {
                    "cms_bench_id": "bench_01",
                    "wireless_stack": "BLE",
                    "board_id": "brd4182a",
                    "part_number": "xg22",
                },
                "firmware": {
                    "board_revision": "a01",
                },
                "test_run": {
                    "test_name": "beacon",
                    "config_name": "default",
                    "config_path": "tests/configs/ble/beacon/full_sweep/no_dcdc/default.json",
                },
            }
        ),
        encoding="utf-8",
    )

    file_name, partition = s3_upload._record_upload_location(result_path)

    assert file_name == "default.json"
    assert partition == "cms/bench_01/xg22/brd4182a_a01//ble/no_dcdc/beacon"

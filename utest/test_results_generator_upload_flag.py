import json
from pathlib import Path
import importlib.util

import pytest

import s3_upload


_RESULTS_GENERATOR_PATH = Path(__file__).resolve().parents[1] / "tests" / "results_generator.py"
_SPEC = importlib.util.spec_from_file_location("results_generator_local_upload_flag", _RESULTS_GENERATOR_PATH)
assert _SPEC is not None and _SPEC.loader is not None
results_generator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(results_generator)

_TESTS_CONFTEST_PATH = Path(__file__).resolve().parents[1] / "tests" / "conftest.py"
_CONFTEST_SPEC = importlib.util.spec_from_file_location(
    "current_measurement_tests_conftest_upload_flag", _TESTS_CONFTEST_PATH
)
assert _CONFTEST_SPEC is not None and _CONFTEST_SPEC.loader is not None
tests_conftest = importlib.util.module_from_spec(_CONFTEST_SPEC)
_CONFTEST_SPEC.loader.exec_module(tests_conftest)


def test_result_generator_uses_correct_upload_env_var(monkeypatch):
    monkeypatch.delenv("CURRRENT_MEASUREMENT_UPLOAD_RESULTS_TO_DATABASE", raising=False)
    monkeypatch.setenv("CURRENT_MEASUREMENT_UPLOAD_RESULTS_TO_DATABASE", "1")

    generator = results_generator.ResultGenerator()

    assert generator.upload_results_to_database is True


def test_result_generator_keeps_misspelled_upload_env_var_compatibility(monkeypatch):
    monkeypatch.delenv("CURRENT_MEASUREMENT_UPLOAD_RESULTS_TO_DATABASE", raising=False)
    monkeypatch.setenv("CURRRENT_MEASUREMENT_UPLOAD_RESULTS_TO_DATABASE", "1")

    generator = results_generator.ResultGenerator()

    assert generator.upload_results_to_database is True


def test_explicit_upload_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("CURRENT_MEASUREMENT_UPLOAD_RESULTS_TO_DATABASE", "1")

    generator = results_generator.ResultGenerator(upload_results_to_database=False)

    assert generator.upload_results_to_database is False


def test_cms_pytest_fixture_disables_environment_upload_without_opt_in(monkeypatch):
    monkeypatch.setenv("CURRENT_MEASUREMENT_UPLOAD_RESULTS_TO_DATABASE", "1")
    captured = {}

    class CapturingResultGenerator:
        def __init__(self, *, upload_results_to_database, retain_captures_for_debug):
            captured["upload_results_to_database"] = upload_results_to_database

    class RequestConfig:
        def getoption(self, option):
            return {
                "--upload-results-to-database": False,
                "--cms-fw-metadata": "cms_fw_metadata.json",
                "--cms-debug": False,
            }[option]

    class Request:
        config = RequestConfig()

    monkeypatch.setattr(tests_conftest, "ResultGenerator", CapturingResultGenerator)
    tests_conftest.ResultGeneratorFixture.__wrapped__(Request())

    assert captured["upload_results_to_database"] is False


def test_result_generator_records_upload_phase_failure(tmp_path, monkeypatch):
    result_json = tmp_path / "beacon_default_result.json"
    result_json.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        results_generator,
        "write_result_json",
        lambda **kwargs: result_json,
    )
    monkeypatch.setattr(results_generator, "_upload_metadata", lambda **kwargs: {})
    monkeypatch.setattr(
        results_generator,
        "upload_result_json_to_database",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    generator = results_generator.ResultGenerator(upload_results_to_database=True)
    generator.set_context(
        test_name="beacon",
        config_name="default",
        config_path=Path("tests/configs/ble/beacon/default.json"),
        config_data={},
        supply_v=2.4,
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        generator.write(
            profile={},
            out_dir=tmp_path,
            artifact_stem="beacon_default",
            result_paths={},
        )

    status = json.loads(
        (tmp_path / "beacon_default_upload_status.json").read_text(encoding="utf-8")
    )
    assert status["status"] == "failed"
    assert status["phase"] == "upload_json"
    assert "database unavailable" in status["error"]


def test_result_storage_metadata_uses_config_and_firmware_identity():
    storage = results_generator._result_storage_metadata(
        test_name="beacon",
        config_name="default",
        config_path=Path("tests/configs/ble/beacon/full_sweep/no_dcdc/default.json"),
        firmware={
            "cms_bench_id": "Timmothy",
            "board": "brd4182a",
            "board_revision": "a01",
            "part_number": "xg22",
            "serial_number": "212300218",
            "sisdk_artifactory_version": "2026.12",
            "sisdk_artifactory_build": "1357",
        },
        slcp_meta={"chip_id": "EFR32MG22"},
    )

    assert storage == {
        "prefix": "cms",
        "cms_bench_id": "Timmothy",
        "protocol": "ble",
        "test_case": "beacon",
        "partition": "no_dcdc",
        "part_number": "xg22",
        "board": "brd4182a",
        "board_revision": "a01",
        "serial_number": "212300218",
        "test_name": "beacon",
        "config_name": "default",
        "sisdk_artifactory_version": "2026.12",
        "sisdk_artifactory_build": "1357",
    }


def test_data_lake_partition_includes_artifactory_build_when_available(tmp_path):
    _, partition = s3_upload._record_upload_location(
        tmp_path / "beacon_result.json",
        cms_bench_id="bench-01",
        sisdk_version="2026.12.0",
        sisdk_artifactory_build="1357",
        part_number="EFR32MG22C224F512IM40",
        board="brd4182a",
        board_revision="a06",
        serial_number="",
        protocol="BLE",
        application_variant="dcdc",
        test_case="beacon",
        measured_at="2026-11-19T12:00:00Z",
    )

    assert partition == (
        "cms/bench-01/2026.12.0/1357/efr32mg22c224f512im40/"
        "brd4182a_a06//ble/dcdc/beacon"
    )


def test_data_lake_partition_omits_absent_artifactory_build(tmp_path):
    _, partition = s3_upload._record_upload_location(
        tmp_path / "beacon_result.json",
        cms_bench_id="bench-01",
        sisdk_version="2026.12.0",
        sisdk_artifactory_build="",
        part_number="EFR32MG22C224F512IM40",
        board="brd4182a",
        board_revision="a06",
        serial_number="",
        protocol="BLE",
        application_variant="dcdc",
        test_case="beacon",
        measured_at="2026-11-19T12:00:00Z",
    )

    assert partition == (
        "cms/bench-01/2026.12.0/efr32mg22c224f512im40/"
        "brd4182a_a06//ble/dcdc/beacon"
    )

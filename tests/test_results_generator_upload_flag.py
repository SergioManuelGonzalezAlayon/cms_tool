from pathlib import Path
import importlib.util


_RESULTS_GENERATOR_PATH = Path(__file__).with_name("results_generator.py")
_SPEC = importlib.util.spec_from_file_location("results_generator_local_upload_flag", _RESULTS_GENERATOR_PATH)
assert _SPEC is not None and _SPEC.loader is not None
results_generator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(results_generator)


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
    }

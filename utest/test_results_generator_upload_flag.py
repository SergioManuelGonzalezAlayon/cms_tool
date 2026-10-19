import json
from pathlib import Path
import importlib.util

import pytest


_RESULTS_GENERATOR_PATH = Path(__file__).resolve().parents[1] / "tests" / "results_generator.py"
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

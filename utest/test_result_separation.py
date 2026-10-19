import importlib.util
import json
from pathlib import Path

from cms_protocol import (
    Artifacts,
    MeasurementResult,
    MeasurementState,
    ResultType,
    artifact_from_dict,
    artifact_to_dict,
    measurement_result_to_dict,
)
from cmsd import CMSDaemon, DaemonConfig


_RESULTS_GENERATOR_PATH = Path(__file__).resolve().parents[1] / "tests" / "results_generator.py"
_SPEC = importlib.util.spec_from_file_location("results_generator_result_types", _RESULTS_GENERATOR_PATH)
assert _SPEC is not None and _SPEC.loader is not None
results_generator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(results_generator)


def test_protocol_keeps_artifact_and_campaign_result_types_explicit():
    artifact = Artifacts(
        name="junit/0001-default.xml",
        result_type=ResultType.PYTEST_JUNIT,
    )

    assert artifact_to_dict(artifact)["result_type"] == "pytest_junit"
    assert artifact_from_dict(artifact_to_dict(artifact)) == artifact
    assert measurement_result_to_dict(
        MeasurementResult(measurement_id="measurement-123", state=MeasurementState.PASSED)
    )["result_type"] == "campaign"


def test_debug_manifest_publishes_overall_result_as_campaign(tmp_path):
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )
    daemon.debug_artifact_measurements.add("measurement-123")

    result = daemon.publish_results(
        MeasurementResult(measurement_id="measurement-123", state=MeasurementState.PASSED)
    )

    manifest_path = (
        daemon.config.artifacts_root
        / "measurements"
        / "measurement-123"
        / "measurement_result.json"
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert measurement_result_to_dict(result)["result_type"] == ResultType.CAMPAIGN.value
    assert payload["result_type"] == "campaign"


def test_generated_json_declares_current_measurement_result_type(tmp_path, monkeypatch):
    config_path = tmp_path / "default.json"
    config_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        results_generator,
        "_capture_policy",
        lambda config_path: {"retained": False, "policy_name": "test"},
    )

    result_path = results_generator.write_result_json(
        profile={"current": {"samples": [0.001, 0.002]}},
        out_dir=tmp_path,
        artifact_stem="beacon_default",
        test_name="beacon",
        config_name="default",
        config_path=config_path,
        config_data={
            "firmware": {
                "sisdk_name": "com.silabs.sdk.stack.sisdk",
                "sisdk_version": "2025.12.3",
                "sisdk_source": "client_config_package_path",
                "sisdk_commit": "sdk-commit",
                "sisdk_hash": "sdk-hash",
                "xapi_file_hash": "xapi-hash",
            }
        },
        result_paths={
            "analyzer": None,
            "note_path": None,
            "consolidated_csv": None,
            "deep_dive_csv": None,
            "html_report": None,
        },
        measured_at="2026-08-18T09:30:00+00:00",
        campaign_id="measurement-123",
        supply_v=3.0,
    )

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["result_type"] == "current_measurement"
    assert payload["dataset"]["sdk_name"] == "com.silabs.sdk.stack.sisdk"
    assert payload["dataset"]["sdk_version"] == "2025.12.3"
    assert payload["firmware"]["dut_sdk_source"] == "client_config_package_path"
    assert payload["firmware"]["dut_xapi_sha256"] == "xapi-hash"

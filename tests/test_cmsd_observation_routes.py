from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

from cms_bench import MeasurementEventStream, MeasurementRuntimePaths
from cms_protocol import (
    CMS_OBSERVATION_PROTOCOL_VERSION,
    Artifacts,
    MeasurementResult,
    MeasurementState,
)
from cmsd import CMSDaemon, DaemonConfig


MEASUREMENT_ID = "measurement-123"


def _daemon(tmp_path: Path) -> CMSDaemon:
    return CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )


def _endpoint(app, path: str):
    return next(route.endpoint for route in app.routes if getattr(route, "path", "") == path)


def _seed_observation(daemon: CMSDaemon) -> Path:
    runtime_dir = daemon.create_runtime_workspace(MEASUREMENT_ID)
    daemon.measurements[MEASUREMENT_ID] = MeasurementResult(
        measurement_id=MEASUREMENT_ID,
        state=MeasurementState.RUNNING,
    )
    job = {
        "job_id": "job-1",
        "ordinal": 1,
        "total_jobs": 1,
        "feature": "beacon",
        "config": "beacon/default.json",
        "config_hash": "hash-1",
        "mode": "default",
        "partition": "",
        "status": "pending",
    }
    (runtime_dir / "orchestrator_plan.json").write_text(
        json.dumps({"record_type": "job_plan", "total_jobs": 1, "jobs": [job]}),
        encoding="utf-8",
    )
    (runtime_dir / "orchestrator_results.jsonl").write_text(
        json.dumps({"record_type": "job_started", "job_id": "job-1", "status": "running"})
        + "\n",
        encoding="utf-8",
    )
    stream = MeasurementEventStream(
        MEASUREMENT_ID,
        MeasurementRuntimePaths.from_root(runtime_dir),
    )
    stream.append_lifecycle("process_started", pid=42)
    stream.append_console("stdout", "ready\n")
    return runtime_dir


def test_active_measurement_endpoint_returns_identity_and_checks_protocol(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    daemon.active_measurement_id = MEASUREMENT_ID
    endpoint = _endpoint(daemon.create_app(), "/measurement/active")

    payload = endpoint(protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION)

    assert payload == {
        "protocol_version": CMS_OBSERVATION_PROTOCOL_VERSION,
        "measurement_id": MEASUREMENT_ID,
    }
    with pytest.raises(HTTPException) as mismatch:
        endpoint(protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION + 1)
    assert mismatch.value.status_code == 409
    assert mismatch.value.detail["code"] == "incompatible_protocol"
    assert mismatch.value.detail["supported_version"] == CMS_OBSERVATION_PROTOCOL_VERSION


def test_summary_and_event_endpoints_serve_separate_structured_payloads(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    _seed_observation(daemon)
    app = daemon.create_app()
    summary_endpoint = _endpoint(app, "/measurement/{measurement_id}/summary")
    events_endpoint = _endpoint(app, "/measurement/{measurement_id}/events")

    summary = summary_endpoint(
        MEASUREMENT_ID,
        protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION,
    )
    events = events_endpoint(
        MEASUREMENT_ID,
        after="1",
        protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION,
    )

    assert summary["measurement_id"] == MEASUREMENT_ID
    assert summary["counts"]["total"] == 1
    assert summary["current_test"]["job_id"] == "job-1"
    assert "events" not in summary
    assert events["measurement_id"] == MEASUREMENT_ID
    assert [event["cursor"] for event in events["events"]] == [2]
    assert events["events"][0]["source"] == "stdout"
    assert events["next_cursor"] == 2
    assert events["oldest_retained_cursor"] == 1


def test_observation_endpoints_return_distinguishable_client_errors(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    app = daemon.create_app()
    summary_endpoint = _endpoint(app, "/measurement/{measurement_id}/summary")
    events_endpoint = _endpoint(app, "/measurement/{measurement_id}/events")

    with pytest.raises(HTTPException) as unknown:
        summary_endpoint("unknown", protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION)
    assert unknown.value.status_code == 404
    assert unknown.value.detail["code"] == "unknown_measurement"

    with pytest.raises(HTTPException) as invalid_cursor:
        events_endpoint(
            "unknown",
            after="-1",
            protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION,
        )
    assert invalid_cursor.value.status_code == 400
    assert invalid_cursor.value.detail["code"] == "invalid_cursor"

    with pytest.raises(HTTPException) as malformed_cursor:
        events_endpoint(
            "unknown",
            after="not-an-integer",
            protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION,
        )
    assert malformed_cursor.value.status_code == 400
    assert malformed_cursor.value.detail["code"] == "invalid_cursor"


def test_summary_endpoint_reports_unavailable_runtime_data(tmp_path: Path, monkeypatch) -> None:
    daemon = _daemon(tmp_path)
    endpoint = _endpoint(daemon.create_app(), "/measurement/{measurement_id}/summary")

    def unavailable(measurement_id: str):
        raise OSError("runtime storage is unavailable")

    monkeypatch.setattr(daemon, "get_measurement_summary", unavailable)

    with pytest.raises(HTTPException) as error:
        endpoint(MEASUREMENT_ID, protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION)
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "observation_unavailable"


def test_explicit_measurement_artifacts_return_identity_and_unknown_error(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    daemon.measurements[MEASUREMENT_ID] = MeasurementResult(
        measurement_id=MEASUREMENT_ID,
        state=MeasurementState.PASSED,
        artifacts=[Artifacts(name="result.json", url="/result.json")],
    )
    endpoint = _endpoint(daemon.create_app(), "/measurement/{measurement_id}/artifacts")

    payload = endpoint(
        MEASUREMENT_ID,
        protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION,
    )

    assert payload["protocol_version"] == CMS_OBSERVATION_PROTOCOL_VERSION
    assert payload["measurement_id"] == MEASUREMENT_ID
    assert payload["artifacts"][0]["name"] == "result.json"

    with pytest.raises(HTTPException) as unknown:
        endpoint("unknown", protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION)
    assert unknown.value.status_code == 404
    assert unknown.value.detail["code"] == "unknown_measurement"


def test_explicit_artifact_file_distinguishes_unknown_measurement_and_artifact(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    endpoint = _endpoint(
        daemon.create_app(),
        "/measurement/{measurement_id}/artifacts/{artifact_path:path}",
    )

    with pytest.raises(HTTPException) as unknown_measurement:
        endpoint("unknown", "result.json")
    assert unknown_measurement.value.status_code == 404
    assert unknown_measurement.value.detail["code"] == "unknown_measurement"

    published_dir = daemon.config.artifacts_root / "measurements" / MEASUREMENT_ID
    published_dir.mkdir(parents=True)
    with pytest.raises(HTTPException) as unknown_artifact:
        endpoint(MEASUREMENT_ID, "missing.json")
    assert unknown_artifact.value.status_code == 404
    assert unknown_artifact.value.detail["code"] == "unknown_artifact"

    artifact = published_dir / "result.json"
    artifact.write_text("{}\n", encoding="utf-8")
    response = endpoint(MEASUREMENT_ID, "result.json")
    assert isinstance(response, FileResponse)
    assert Path(response.path) == artifact

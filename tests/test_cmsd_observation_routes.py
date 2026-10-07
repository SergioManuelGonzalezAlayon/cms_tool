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


def test_debug_artifacts_recover_manifestless_measurement_logs(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    published_dir = daemon.config.artifacts_root / "measurements" / MEASUREMENT_ID
    published_dir.mkdir(parents=True)
    for name, content in (
        ("pytest.log", "pytest failed before publication\n"),
        ("test_stderr.log", "daemon traceback\n"),
        ("test_stdout.log", "test output\n"),
    ):
        (published_dir / name).write_text(content, encoding="utf-8")

    app = daemon.create_app()
    listing_endpoint = _endpoint(app, "/artifacts/measurements")
    debug_endpoint = _endpoint(app, "/measurement/{measurement_id}/artifacts")
    file_endpoint = _endpoint(
        app,
        "/measurement/{measurement_id}/artifacts/{artifact_path:path}",
    )

    saved = listing_endpoint(protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION)
    assert saved["measurements"][0]["measurement_id"] == MEASUREMENT_ID
    assert saved["measurements"][0]["has_manifest"] is False
    assert saved["measurements"][0]["artifact_count"] == 3

    debug_payload = debug_endpoint(
        MEASUREMENT_ID,
        storage="debug",
        protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION,
    )
    assert {artifact["name"] for artifact in debug_payload["artifacts"]} == {
        "pytest.log",
        "test_stderr.log",
        "test_stdout.log",
    }

    response = file_endpoint(MEASUREMENT_ID, "test_stdout.log", storage="debug")
    assert isinstance(response, FileResponse)
    assert Path(response.path) == published_dir / "test_stdout.log"


def test_bench_artifact_scopes_list_files_and_reject_traversal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts" / "cmsd",
            bench_config=tmp_path / "bench.json",
        )
    )
    cmsd_artifact = daemon.config.artifacts_root / "cmsd.log"
    cmsd_artifact.parent.mkdir(parents=True)
    cmsd_artifact.write_text("cmsd\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    orchestrator_artifact = tmp_path / "artifacts" / "orchestrator" / "results.jsonl"
    orchestrator_artifact.parent.mkdir(parents=True)
    orchestrator_artifact.write_text("{}\n", encoding="utf-8")

    app = daemon.create_app()
    listing_endpoint = _endpoint(app, "/bench/artifacts/{scope}")
    file_endpoint = _endpoint(app, "/bench/artifacts/{scope}/{artifact_path:path}")

    payload = listing_endpoint(
        "orchestrator",
        protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION,
    )
    assert [artifact["name"] for artifact in payload["artifacts"]] == ["results.jsonl"]
    response = file_endpoint("cmsd", "cmsd.log")
    assert isinstance(response, FileResponse)
    assert Path(response.path) == cmsd_artifact

    with pytest.raises(HTTPException) as traversal:
        file_endpoint("cmsd", "../outside.log")
    assert traversal.value.status_code == 400
    assert traversal.value.detail["code"] == "invalid_artifact_path"


def test_measurement_status_prefers_active_runtime_status(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    daemon.active_measurement_id = MEASUREMENT_ID
    daemon.measurements[MEASUREMENT_ID] = MeasurementResult(
        measurement_id=MEASUREMENT_ID,
        state=MeasurementState.ACCEPTED,
        progress_percent=5,
        current_step="accepted",
        message="stale memory state",
    )
    runtime_dir = daemon.create_runtime_workspace(MEASUREMENT_ID)
    (runtime_dir / "current_status.json").write_text(
        json.dumps(
            {
                "measurement_id": MEASUREMENT_ID,
                "state": "running",
                "progress_percent": 42,
                "current_step": "pytest",
                "message": "runtime status",
            }
        ),
        encoding="utf-8",
    )
    endpoint = _endpoint(daemon.create_app(), "/measurement/{measurement_id}")

    payload = endpoint(MEASUREMENT_ID)

    assert payload["state"] == "running"
    assert payload["progress_percent"] == 42
    assert payload["current_step"] == "pytest"
    assert payload["message"] == "runtime status"


def test_measurement_status_prefers_published_result_for_completed_measurement(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    daemon.measurements[MEASUREMENT_ID] = MeasurementResult(
        measurement_id=MEASUREMENT_ID,
        state=MeasurementState.RUNNING,
        progress_percent=40,
        current_step="running",
        message="stale memory state",
    )
    published_dir = daemon.config.artifacts_root / "measurements" / MEASUREMENT_ID
    published_dir.mkdir(parents=True)
    (published_dir / "measurement_result.json").write_text(
        json.dumps(
            {
                "measurement_id": MEASUREMENT_ID,
                "state": "passed",
                "progress_percent": 100,
                "current_step": "passed",
                "message": "published final result",
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    endpoint = _endpoint(daemon.create_app(), "/measurement/{measurement_id}")

    payload = endpoint(MEASUREMENT_ID)

    assert payload["state"] == "passed"
    assert payload["progress_percent"] == 100
    assert payload["message"] == "published final result"


def test_daemon_startup_reconstructs_completed_published_results(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    older = daemon.config.artifacts_root / "measurements" / "older"
    newer = daemon.config.artifacts_root / "measurements" / "newer"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    (older / "measurement_result.json").write_text(
        json.dumps(
            {
                "measurement_id": "older",
                "state": "failed",
                "finished_at": "2026-08-18T09:00:00+00:00",
                "progress_percent": 100,
                "current_step": "failed",
                "message": "older result",
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    (newer / "measurement_result.json").write_text(
        json.dumps(
            {
                "measurement_id": "newer",
                "state": "passed",
                "finished_at": "2026-08-18T10:00:00+00:00",
                "progress_percent": 100,
                "current_step": "passed",
                "message": "newer result",
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )

    daemon.reconstruct_completed_results()

    assert daemon.measurements["older"].state is MeasurementState.FAILED
    assert daemon.measurements["newer"].state is MeasurementState.PASSED
    assert daemon.last_measurement_id == "newer"


def test_completed_measurement_discovery_is_limited_to_newest_published_results(
    tmp_path: Path,
) -> None:
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
            completed_measurement_retention=2,
        )
    )
    for index, measurement_id in enumerate(("oldest", "middle", "newest"), start=1):
        published_dir = daemon.config.artifacts_root / "measurements" / measurement_id
        published_dir.mkdir(parents=True)
        (published_dir / "measurement_result.json").write_text(
            json.dumps(
                {
                    "measurement_id": measurement_id,
                    "state": "passed",
                    "finished_at": f"2026-08-18T0{index}:00:00+00:00",
                    "progress_percent": 100,
                    "current_step": "passed",
                    "message": f"{measurement_id} result",
                    "artifacts": [],
                }
            ),
            encoding="utf-8",
        )

    daemon.reconstruct_completed_results()
    endpoint = _endpoint(daemon.create_app(), "/measurements/completed")
    payload = endpoint(protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION)

    assert list(daemon.measurements) == ["newest", "middle"]
    assert daemon.last_measurement_id == "newest"
    assert [item["measurement_id"] for item in payload["measurements"]] == ["newest", "middle"]
    assert payload["retention_limit"] == 2
    assert (
        daemon.config.artifacts_root
        / "measurements"
        / "oldest"
        / "measurement_result.json"
    ).exists()


def test_daemon_startup_marks_unfinished_runtime_workspace_interrupted(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    daemon.debug_artifact_measurements.add(MEASUREMENT_ID)
    runtime_dir = daemon.config.runtime_root / "measurements" / MEASUREMENT_ID
    runtime_dir.mkdir(parents=True)
    job = {
        "job_id": "job-1",
        "ordinal": 1,
        "total_jobs": 1,
        "feature": "beacon",
        "config": "dcdc.json",
        "config_hash": "hash-dcdc",
        "mode": "default",
        "partition": "",
        "status": "pending",
    }
    (runtime_dir / "current_status.json").write_text(
        json.dumps(
            {
                "measurement_id": MEASUREMENT_ID,
                "state": "running",
                "progress_percent": 70,
                "current_step": "running measurement",
            }
        ),
        encoding="utf-8",
    )
    (runtime_dir / "orchestrator_plan.json").write_text(
        json.dumps({"record_type": "job_plan", "total_jobs": 1, "jobs": [job]}),
        encoding="utf-8",
    )
    (runtime_dir / "orchestrator_results.jsonl").write_text(
        json.dumps({"record_type": "job_started", "job_id": "job-1", "status": "running"})
        + "\n",
        encoding="utf-8",
    )

    daemon.reconstruct_interrupted_runtime_workspaces()

    result = daemon.get_measurement_result(MEASUREMENT_ID)
    assert result.state is MeasurementState.INTERRUPTED
    assert result.progress_percent == 70
    assert result.current_step == "interrupted"
    assert "process was not reattached" in result.message
    assert daemon.active_measurement_id is None
    assert MEASUREMENT_ID not in daemon.cancellation_events
    published_result = daemon.config.artifacts_root / "measurements" / MEASUREMENT_ID / "measurement_result.json"
    assert json.loads(published_result.read_text(encoding="utf-8"))["state"] == "interrupted"

    summary = _endpoint(daemon.create_app(), "/measurement/{measurement_id}/summary")(
        MEASUREMENT_ID,
        protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION,
    )
    assert summary["state"] == "interrupted"
    assert summary["counts"]["total"] == 1
    assert summary["current_test"]["job_id"] == "job-1"


def test_daemon_initialize_marks_unfinished_workspace_interrupted(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    daemon.debug_artifact_measurements.add(MEASUREMENT_ID)
    runtime_dir = _seed_observation(daemon)
    (runtime_dir / "current_status.json").write_text(
        json.dumps(
            {
                "measurement_id": MEASUREMENT_ID,
                "state": "running",
                "progress_percent": 45,
                "current_step": "running measurement",
            }
        ),
        encoding="utf-8",
    )

    daemon.initialize()

    result = daemon.get_measurement_result(MEASUREMENT_ID)
    assert result.state is MeasurementState.INTERRUPTED
    assert result.progress_percent == 45
    published_result = (
        daemon.config.artifacts_root
        / "measurements"
        / MEASUREMENT_ID
        / "measurement_result.json"
    )
    assert json.loads(published_result.read_text(encoding="utf-8"))["state"] == "interrupted"


def test_daemon_startup_does_not_interrupt_published_or_staging_only_workspaces(
    tmp_path: Path,
) -> None:
    daemon = _daemon(tmp_path)
    published_runtime = daemon.config.runtime_root / "measurements" / "already-published"
    published_runtime.mkdir(parents=True)
    (published_runtime / "current_status.json").write_text(
        json.dumps({"state": "running"}),
        encoding="utf-8",
    )
    published_dir = daemon.config.artifacts_root / "measurements" / "already-published"
    published_dir.mkdir(parents=True)
    (published_dir / "measurement_result.json").write_text(
        json.dumps(
            {
                "measurement_id": "already-published",
                "state": "passed",
                "progress_percent": 100,
                "current_step": "passed",
                "message": "published final result",
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    staging_only = daemon.config.runtime_root / "measurements" / "staging-only"
    staging_only.mkdir(parents=True)
    (staging_only / "dut.s37").write_text("firmware", encoding="utf-8")

    daemon.reconstruct_completed_results()
    daemon.reconstruct_interrupted_runtime_workspaces()

    assert daemon.measurements["already-published"].state is MeasurementState.PASSED
    assert "staging-only" not in daemon.measurements


def test_summary_endpoint_reads_published_run_files_without_memory_state(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    published_dir = daemon.config.artifacts_root / "measurements" / MEASUREMENT_ID
    published_dir.mkdir(parents=True)
    job = {
        "job_id": "job-1",
        "ordinal": 1,
        "total_jobs": 1,
        "feature": "beacon",
        "config": "dcdc.json",
        "config_hash": "hash-dcdc",
        "mode": "one_factor",
        "partition": "",
        "status": "pending",
    }
    (published_dir / "orchestrator_plan.json").write_text(
        json.dumps({"record_type": "job_plan", "total_jobs": 1, "jobs": [job]}),
        encoding="utf-8",
    )
    (published_dir / "orchestrator_results.jsonl").write_text(
        json.dumps(
            {
                "record_type": "job_completed",
                "job_id": "job-1",
                "status": "failed",
                "timestamp": "2026-08-18T10:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (published_dir / "measurement_result.json").write_text(
        json.dumps(
            {
                "measurement_id": MEASUREMENT_ID,
                "state": "failed",
                "progress_percent": 100,
                "current_step": "failed",
                "message": "published final result",
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    endpoint = _endpoint(daemon.create_app(), "/measurement/{measurement_id}/summary")

    payload = endpoint(MEASUREMENT_ID, protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION)

    assert payload["state"] == "failed"
    assert payload["counts"]["total"] == 1
    assert payload["counts"]["failed"] == 1
    assert payload["failed_tests"][0]["config"] == "dcdc.json"


def test_events_endpoint_reads_published_job_records_without_memory_state(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    published_dir = daemon.config.artifacts_root / "measurements" / MEASUREMENT_ID
    published_dir.mkdir(parents=True)
    (published_dir / "orchestrator_results.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "record_type": "job_started",
                        "job_id": "job-1",
                        "status": "running",
                        "timestamp": "2026-08-18T09:59:00+00:00",
                    }
                ),
                json.dumps(
                    {
                        "record_type": "job_completed",
                        "job_id": "job-1",
                        "status": "failed",
                        "timestamp": "2026-08-18T10:00:00+00:00",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    endpoint = _endpoint(daemon.create_app(), "/measurement/{measurement_id}/events")

    payload = endpoint(
        MEASUREMENT_ID,
        after="1",
        protocol_version=CMS_OBSERVATION_PROTOCOL_VERSION,
    )

    assert payload["measurement_id"] == MEASUREMENT_ID
    assert [event["cursor"] for event in payload["events"]] == [2]
    assert payload["events"][0]["type"] == "lifecycle"
    assert payload["events"][0]["state"] == "job_completed"
    assert payload["events"][0]["status"] == "failed"
    assert payload["next_cursor"] == 2

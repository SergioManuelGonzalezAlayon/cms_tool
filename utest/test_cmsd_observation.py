from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cms_bench import MeasurementEventStream, MeasurementRuntimePaths
from cms_protocol import (
    EventCursor,
    MeasurementIdentity,
    MeasurementResult,
    MeasurementState,
)
from cmsd import CMSDaemon, DaemonConfig


MEASUREMENT_ID = "measurement-123"


def _daemon(tmp_path: Path, *, max_console_event_bytes: int = 4096) -> CMSDaemon:
    return CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
            max_console_event_bytes=max_console_event_bytes,
        )
    )


def _plan_job(job_id: str, ordinal: int, total_jobs: int) -> dict:
    return {
        "job_id": job_id,
        "ordinal": ordinal,
        "total_jobs": total_jobs,
        "feature": "beacon",
        "config": f"beacon/job-{ordinal}.json",
        "config_hash": f"hash-{ordinal}",
        "mode": "default",
        "partition": "",
        "status": "pending",
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_daemon_resolves_active_measurement_from_existing_runtime_state(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)

    assert daemon.get_active_measurement_id() is None

    daemon.active_measurement_id = MEASUREMENT_ID

    assert daemon.get_active_measurement_id() == MEASUREMENT_ID


def test_daemon_reconstructs_summary_from_plan_and_append_only_results(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    runtime_dir = daemon.create_runtime_workspace(MEASUREMENT_ID)
    identity = MeasurementIdentity(
        measurement_id=MEASUREMENT_ID,
        run_name="Beacon Run",
        created_at=datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc),
    )
    daemon.measurements[MEASUREMENT_ID] = MeasurementResult(
        measurement_id=MEASUREMENT_ID,
        state=MeasurementState.RUNNING,
        identity=identity,
    )
    jobs = [_plan_job(f"job-{ordinal}", ordinal, 3) for ordinal in range(1, 4)]
    (runtime_dir / "orchestrator_plan.json").write_text(
        json.dumps({"record_type": "job_plan", "total_jobs": 3, "jobs": jobs}),
        encoding="utf-8",
    )
    _write_jsonl(
        runtime_dir / "orchestrator_results.jsonl",
        [
            {"record_type": "job_started", "job_id": "job-1", "status": "running"},
            {"record_type": "job_completed", "job_id": "job-1", "status": "passed"},
            {"record_type": "job_started", "job_id": "job-2", "status": "running"},
            {"record_type": "job_completed", "job_id": "job-2", "status": "failed"},
            {"record_type": "job_started", "job_id": "job-3", "status": "running"},
        ],
    )

    summary = daemon.get_measurement_summary(MEASUREMENT_ID)

    assert summary.identity == identity
    assert summary.state is MeasurementState.RUNNING
    assert summary.counts.total == 3
    assert summary.counts.completed == 2
    assert summary.counts.passed == 1
    assert summary.counts.failed == 1
    assert summary.counts.skipped == 0
    assert summary.counts.remaining == 1
    assert summary.current_test is not None
    assert summary.current_test.job_id == "job-3"
    assert [test.job_id for test in summary.failed_tests] == ["job-2"]


def test_daemon_summary_uses_latest_job_state_without_reading_console_text(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    runtime_dir = daemon.create_runtime_workspace(MEASUREMENT_ID)
    daemon.measurements[MEASUREMENT_ID] = MeasurementResult(
        measurement_id=MEASUREMENT_ID,
        state=MeasurementState.RUNNING,
    )
    jobs = [_plan_job("job-1", 1, 1)]
    (runtime_dir / "orchestrator_plan.json").write_text(
        json.dumps({"record_type": "job_plan", "total_jobs": 1, "jobs": jobs}),
        encoding="utf-8",
    )
    _write_jsonl(
        runtime_dir / "orchestrator_results.jsonl",
        [
            {"job_id": "job-1", "status": "failed"},
            {"record_type": "job_started", "job_id": "job-1", "status": "running"},
            {"record_type": "job_completed", "job_id": "job-1", "status": "passed"},
        ],
    )
    (runtime_dir / "test_stdout.log").write_text("FAILED fake console text\n", encoding="utf-8")

    summary = daemon.get_measurement_summary(MEASUREMENT_ID)

    assert summary.counts.completed == 1
    assert summary.counts.passed == 1
    assert summary.counts.failed == 0
    assert summary.current_test is None
    assert summary.failed_tests == ()


def test_daemon_returns_events_after_cursor_in_global_order(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    runtime_dir = daemon.create_runtime_workspace(MEASUREMENT_ID)
    stream = MeasurementEventStream(
        MEASUREMENT_ID,
        MeasurementRuntimePaths.from_root(runtime_dir),
        max_console_bytes=daemon.config.max_console_event_bytes,
    )
    stream.append_lifecycle("process_started", pid=42)
    stream.append_console("stdout", "first line\n")
    stream.append_console("stderr", "warning line\n")
    stream.append_lifecycle("process_completed", returncode=0)

    page = daemon.get_measurement_events(MEASUREMENT_ID, EventCursor(1))

    assert [event.cursor.value for event in page.events] == [2, 3, 4]
    assert [event.event_type for event in page.events] == ["console", "console", "lifecycle"]
    assert page.events[0].details["source"] == "stdout"
    assert page.events[1].details["source"] == "stderr"
    assert page.next_cursor == EventCursor(4)
    assert page.oldest_retained_cursor == EventCursor(1)
    assert page.truncated is False


def test_daemon_reports_console_truncation_but_keeps_structured_events(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, max_console_event_bytes=700)
    runtime_dir = daemon.create_runtime_workspace(MEASUREMENT_ID)
    stream = MeasurementEventStream(
        MEASUREMENT_ID,
        MeasurementRuntimePaths.from_root(runtime_dir),
        max_console_bytes=daemon.config.max_console_event_bytes,
    )
    stream.append_lifecycle("process_started", pid=42)
    for index in range(50):
        stream.append_console("stdout", f"line-{index}-" + ("x" * 80) + "\n")

    page = daemon.get_measurement_events(MEASUREMENT_ID, EventCursor())

    assert page.truncated is True
    assert page.oldest_retained_cursor.value > 1
    assert any(event.details.get("state") == "process_started" for event in page.events)
    assert any(event.details.get("state") == "console_truncated" for event in page.events)
    assert page.next_cursor == page.events[-1].cursor


def test_daemon_observation_rejects_unknown_measurement(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)

    with pytest.raises(KeyError, match="measurement id is unknown"):
        daemon.get_measurement_summary("unknown")
    with pytest.raises(KeyError, match="measurement id is unknown"):
        daemon.get_measurement_events("unknown")

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

import pytest

import cms_bench
from cms_bench import MeasurementEventStream, MeasurementRuntimePaths


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_event_stream_appends_lifecycle_and_console_records_with_monotonic_cursors(
    tmp_path: Path,
) -> None:
    paths = MeasurementRuntimePaths.from_root(tmp_path)
    stream = MeasurementEventStream("measurement-123", paths)

    stream.append_lifecycle("process_started", pid=42)
    stream.append_console("stdout", "first line\n")
    stream.append_console("stderr", "warning line\n")

    records = stream.read_events()
    assert [record["cursor"] for record in records] == [1, 2, 3]
    assert [record["type"] for record in records] == ["lifecycle", "console", "console"]
    assert records[0]["state"] == "process_started"
    assert records[1]["source"] == "stdout"
    assert records[1]["text"] == "first line\n"
    assert records[2]["source"] == "stderr"
    assert all(record["measurement_id"] == "measurement-123" for record in records)
    assert all(datetime.fromisoformat(record["timestamp"]).utcoffset() is not None for record in records)


def test_event_stream_serializes_concurrent_cursor_allocation(tmp_path: Path) -> None:
    paths = MeasurementRuntimePaths.from_root(tmp_path)
    stream = MeasurementEventStream("measurement-123", paths)

    def append_records(source: str) -> None:
        for index in range(50):
            stream.append_console(source, f"{source}-{index}\n")

    workers = [
        threading.Thread(target=append_records, args=(source,))
        for source in ("stdout", "stderr")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    records = stream.read_events()
    assert [record["cursor"] for record in records] == list(range(1, 101))
    assert {record["source"] for record in records} == {"stdout", "stderr"}


def test_event_stream_resumes_after_last_existing_cursor(tmp_path: Path) -> None:
    paths = MeasurementRuntimePaths.from_root(tmp_path)
    first_stream = MeasurementEventStream("measurement-123", paths)
    first_stream.append_lifecycle("process_started")
    first_stream.append_console("stdout", "output\n")

    resumed_stream = MeasurementEventStream("measurement-123", paths)
    resumed_stream.append_lifecycle("process_completed", returncode=0)

    assert [record["cursor"] for record in resumed_stream.read_events()] == [1, 2, 3]


def test_current_status_is_replaced_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = MeasurementRuntimePaths.from_root(tmp_path)
    stream = MeasurementEventStream("measurement-123", paths)
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def record_replace(source, destination) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(cms_bench.os, "replace", record_replace)
    stream.append_lifecycle("process_started")
    stream.replace_status({"state": "running", "pid": 42})
    stream.append_lifecycle("process_completed", returncode=0)
    stream.replace_status({"state": "process_completed", "returncode": 0})

    status = json.loads(paths.status.read_text(encoding="utf-8"))
    assert status["measurement_id"] == "measurement-123"
    assert status["state"] == "process_completed"
    assert status["returncode"] == 0
    assert status["cursor"] == 2
    assert datetime.fromisoformat(status["updated_at"]).utcoffset() is not None
    assert len(replacements) == 2
    assert all(destination == paths.status for _, destination in replacements)
    assert not paths.status.with_name(paths.status.name + ".tmp").exists()

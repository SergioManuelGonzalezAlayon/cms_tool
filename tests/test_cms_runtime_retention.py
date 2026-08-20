from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cms_bench import MeasurementEventStream, MeasurementRuntimePaths, _run_managed_process
from cms_protocol import FirmwareData, FirmwareImage, MeasurementRequest, SuiteSelector
from cmsd import CMSDaemon, DaemonConfig


FAKE_CHILD = Path(__file__).parent / "helpers" / "fake_child_process.py"


def test_console_event_segments_are_bounded_and_keep_structured_records(tmp_path: Path) -> None:
    paths = MeasurementRuntimePaths.from_root(tmp_path)
    stream = MeasurementEventStream("measurement-123", paths, max_console_bytes=900)
    stream.append_lifecycle("process_started", pid=42)

    for index in range(80):
        stream.append_console("stdout", f"line-{index:03d}-" + ("x" * 80) + "\n")

    retained = stream.read_events()
    structured = [
        json.loads(line)
        for line in paths.events.read_text(encoding="utf-8").splitlines()
    ]
    console_records = [record for record in retained if record["type"] == "console"]
    segment_bytes = sum(path.stat().st_size for path in paths.console_events.glob("*.jsonl"))

    assert segment_bytes <= 900
    assert any(record.get("state") == "process_started" for record in structured)
    assert any(record.get("state") == "console_truncated" for record in structured)
    assert not any("line-000" in record["text"] for record in console_records)
    assert any("line-079" in record["text"] for record in console_records)
    assert stream.oldest_retained_cursor > 1
    assert [record["cursor"] for record in retained] == sorted(
        record["cursor"] for record in retained
    )


def test_status_records_oldest_retained_cursor_after_console_truncation(tmp_path: Path) -> None:
    paths = MeasurementRuntimePaths.from_root(tmp_path)
    stream = MeasurementEventStream("measurement-123", paths, max_console_bytes=700)
    for index in range(50):
        stream.append_console("stderr", f"warning-{index}-" + ("y" * 80) + "\n")

    stream.replace_status({"state": "running"})

    status = json.loads(paths.status.read_text(encoding="utf-8"))
    assert status["oldest_retained_cursor"] == stream.oldest_retained_cursor
    assert status["oldest_retained_cursor"] > 1


def test_console_truncation_does_not_modify_structured_job_files(tmp_path: Path) -> None:
    paths = MeasurementRuntimePaths.from_root(tmp_path)
    plan_path = tmp_path / "orchestrator_plan.json"
    results_path = tmp_path / "orchestrator_results.jsonl"
    plan_content = '{"jobs":[{"job_id":"job-1"}]}\n'
    results_content = '{"job_id":"job-1","status":"passed"}\n'
    plan_path.write_text(plan_content, encoding="utf-8")
    results_path.write_text(results_content, encoding="utf-8")
    stream = MeasurementEventStream("measurement-123", paths, max_console_bytes=700)

    for index in range(50):
        stream.append_console("stdout", f"verbose-{index}-" + ("z" * 80) + "\n")

    assert plan_path.read_text(encoding="utf-8") == plan_content
    assert results_path.read_text(encoding="utf-8") == results_content


def test_managed_process_bounds_raw_logs_and_records_truncation(tmp_path: Path) -> None:
    paths = MeasurementRuntimePaths.from_root(tmp_path)
    stream = MeasurementEventStream("measurement-123", paths, max_console_bytes=1200)
    command = [
        sys.executable,
        str(FAKE_CHILD),
        "--action",
        "stdout-repeat:400",
        "--action",
        "stderr-repeat:400",
    ]

    completed = _run_managed_process(
        command,
        cwd=Path("."),
        stdout_path=paths.stdout,
        stderr_path=paths.stderr,
        event_stream=stream,
        log_max_bytes=1024,
    )

    assert isinstance(completed, subprocess.CompletedProcess)
    assert completed.returncode == 0
    assert paths.stdout.stat().st_size <= 1024
    assert paths.stderr.stat().st_size <= 1024
    assert "stdout-399" in paths.stdout.read_text(encoding="utf-8")
    assert "stderr-399" in paths.stderr.read_text(encoding="utf-8")
    lifecycle = [record for record in stream.read_events() if record["type"] == "lifecycle"]
    truncated_sources = {
        record["source"]
        for record in lifecycle
        if record.get("state") == "log_truncated"
    }
    assert truncated_sources == {"stdout", "stderr"}


def test_daemon_propagates_configured_console_bounds_to_bench_session(tmp_path: Path) -> None:
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
            max_console_log_bytes=1234,
            max_console_event_bytes=5678,
        )
    )
    request = MeasurementRequest(
        request_id="measurement-123",
        selector=SuiteSelector(mode="ble", feature="beacon", config="default"),
        firmware=FirmwareData(
            dut_img=FirmwareImage(name="dut.s37", url=""),
            tester_img=FirmwareImage(name="tester.s37", url=""),
        ),
    )
    runtime_dir = daemon.create_runtime_workspace(request.request_id)

    session = daemon.build_session(request, runtime_dir)

    assert session.max_console_log_bytes == 1234
    assert session.max_console_event_bytes == 5678

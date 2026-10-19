from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

import cmsd
from cms_bench import BenchSession, CMSBench, _run_managed_process
from cms_protocol import (
    Artifacts,
    FirmwareData,
    FirmwareImage,
    MeasurementIdentity,
    MeasurementRequest,
    MeasurementResult,
    MeasurementState,
    ResultType,
    SuiteSelector,
)
from cmsd import CMSDaemon, DaemonConfig


FAKE_CHILD = Path(__file__).resolve().parents[1] / "tests" / "helpers" / "fake_child_process.py"


def _request(measurement_id: str) -> MeasurementRequest:
    identity = MeasurementIdentity(
        measurement_id=measurement_id,
        run_name="Publication test",
        created_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    firmware = FirmwareData(
        dut_img=FirmwareImage(name="dut.s37", url=""),
        tester_img=FirmwareImage(name="tester.s37", url=""),
    )
    return MeasurementRequest(
        request_id=measurement_id,
        selector=SuiteSelector(mode="ble", feature="beacon", config="default"),
        firmware=firmware,
        identity=identity,
    )


def test_default_linux_runtime_root_targets_shared_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cmsd.platform, "system", lambda: "Linux")

    assert cmsd._default_runtime_root() == Path("/dev/shm/cmsd")


def test_daemon_config_rejects_runtime_beneath_persistent_artifacts(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"

    with pytest.raises(ValueError, match="must not overlap"):
        DaemonConfig(
            runtime_root=artifacts_root / "runtime",
            artifacts_root=artifacts_root,
        )


def test_daemon_publishes_only_selected_final_artifacts(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    artifacts_root = tmp_path / "artifacts"
    runtime_dir = runtime_root / "measurements" / "measurement-123"
    runtime_dir.mkdir(parents=True)
    junit = runtime_dir / "junit" / "0001-default.xml"
    junit.parent.mkdir()
    current_result = runtime_dir / "beacon_result.json"
    selected_log = runtime_dir / "test_stdout.log"
    status = runtime_dir / "current_status.json"
    events = runtime_dir / "events.jsonl"
    console_segment = runtime_dir / "console_events" / "00000000000000000001.jsonl"
    console_segment.parent.mkdir()
    for path, content in (
        (junit, "<testsuites />\n"),
        (current_result, '{"result_type":"current_measurement"}\n'),
        (selected_log, "final output\n"),
        (status, '{"state":"process_completed"}\n'),
        (events, '{"type":"lifecycle"}\n'),
        (console_segment, '{"type":"console"}\n'),
    ):
        path.write_text(content, encoding="utf-8")
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=runtime_root,
            artifacts_root=artifacts_root,
            bench_config=tmp_path / "bench.json",
        )
    )
    result = MeasurementResult(
        measurement_id="measurement-123",
        state=MeasurementState.PASSED,
        artifacts=[
            Artifacts(name="cmsd/measurement-123/test_stdout.log", path=selected_log),
            Artifacts(name="cmsd/measurement-123/current_status.json", path=status),
            Artifacts(name="cmsd/measurement-123/events.jsonl", path=events),
            Artifacts(name="cmsd/measurement-123/console.jsonl", path=console_segment),
            Artifacts(
                name="cmsd/measurement-123/junit/0001-default.xml",
                path=junit,
                result_type=ResultType.PYTEST_JUNIT,
            ),
            Artifacts(
                name="beacon_result.json",
                path=current_result,
                result_type=ResultType.CURRENT_MEASUREMENT,
            ),
        ],
    )

    published = daemon.publish_results(result)

    published_names = {artifact.name for artifact in published.artifacts}
    assert "cmsd/measurement-123/test_stdout.log" in published_names
    assert "cmsd/measurement-123/junit/0001-default.xml" in published_names
    assert "beacon_result.json" in published_names
    assert not any(name.endswith("current_status.json") for name in published_names)
    assert not any(name.endswith("events.jsonl") for name in published_names)
    assert not any(name.endswith("console.jsonl") for name in published_names)
    assert status.exists() and events.exists() and console_segment.exists()


def test_final_child_output_is_flushed_before_publication(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    artifacts_root = tmp_path / "artifacts"
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=runtime_root,
            artifacts_root=artifacts_root,
            bench_config=tmp_path / "bench.json",
        )
    )
    request = _request("measurement-123")
    runtime_dir = daemon.create_runtime_workspace(request.request_id)
    session = daemon.build_session(request, runtime_dir)
    command = [
        sys.executable,
        str(FAKE_CHILD),
        "--action",
        "stdout:final stdout line",
        "--action",
        "stderr:final stderr line",
    ]
    completed = _run_managed_process(
        command,
        cwd=Path("."),
        stdout_path=session.runtime_paths.stdout,
        stderr_path=session.runtime_paths.stderr,
        event_stream=session.event_stream,
        log_max_bytes=session.max_console_log_bytes,
    )
    monkeypatch.setattr("cms_bench._real_test_artifacts", lambda session, since=None: [])
    result = CMSBench().collect_artifacts(
        session,
        MeasurementResult(
            measurement_id=request.request_id,
            state=MeasurementState.PASSED,
            identity=request.identity,
        ),
    )

    published = daemon.publish_results(result)

    assert completed.returncode == 0
    published_stdout = next(
        artifact.path
        for artifact in published.artifacts
        if artifact.name.endswith("test_stdout.log")
    )
    published_stderr = next(
        artifact.path
        for artifact in published.artifacts
        if artifact.name.endswith("test_stderr.log")
    )
    assert published_stdout is not None
    assert published_stderr is not None
    assert published_stdout.read_text(encoding="utf-8") == "final stdout line\n"
    assert published_stderr.read_text(encoding="utf-8") == "final stderr line\n"
    assert session.runtime_paths.stdout.is_relative_to(runtime_root)
    assert not session.runtime_paths.stdout.is_relative_to(artifacts_root)
    assert session.runtime_paths.events.exists()
    assert not any(path.name == "events.jsonl" for path in artifacts_root.rglob("*"))

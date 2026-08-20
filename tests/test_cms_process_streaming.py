from __future__ import annotations

import subprocess
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import cms_bench
from cms_bench import BenchSession, MeasurementEventStream, MeasurementRuntimePaths
from cms_protocol import (
    FirmwareData,
    FirmwareImage,
    MeasurementIdentity,
    MeasurementRequest,
    SuiteSelector,
)


FAKE_CHILD = Path(__file__).parent / "helpers" / "fake_child_process.py"


def _fake_command(*actions: str) -> list[str]:
    command = [sys.executable, str(FAKE_CHILD)]
    for action in actions:
        command.extend(["--action", action])
    return command


def _run_fake(tmp_path: Path, *actions: str) -> subprocess.CompletedProcess:
    event_stream = MeasurementEventStream(
        "measurement-123",
        MeasurementRuntimePaths.from_root(tmp_path),
    )
    return cms_bench._run_managed_process(
        _fake_command(*actions),
        cwd=Path("."),
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        event_stream=event_stream,
    )


def test_managed_process_exposes_output_before_child_exit(tmp_path: Path) -> None:
    outcomes: list[subprocess.CompletedProcess] = []
    worker = threading.Thread(
        target=lambda: outcomes.append(
            _run_fake(
                tmp_path,
                "stdout:visible-before-exit",
                "pause:0.75",
                "stdout:visible-after-pause",
            )
        ),
        daemon=True,
    )
    worker.start()

    stdout_path = tmp_path / "stdout.log"
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if stdout_path.exists() and "visible-before-exit" in stdout_path.read_text(encoding="utf-8"):
            break
        time.sleep(0.01)

    assert "visible-before-exit" in stdout_path.read_text(encoding="utf-8")
    assert worker.is_alive()
    worker.join(timeout=5.0)
    assert not worker.is_alive()
    assert outcomes[0].returncode == 0


def test_managed_process_drains_stdout_and_stderr(tmp_path: Path) -> None:
    completed = _run_fake(
        tmp_path,
        "stdout:stdout-first",
        "stderr:stderr-first",
        "stdout:stdout-second",
        "stderr:stderr-second",
    )

    assert completed.returncode == 0
    assert (tmp_path / "stdout.log").read_text(encoding="utf-8").splitlines() == [
        "stdout-first",
        "stdout-second",
    ]
    assert (tmp_path / "stderr.log").read_text(encoding="utf-8").splitlines() == [
        "stderr-first",
        "stderr-second",
    ]
    events = MeasurementEventStream(
        "measurement-123",
        MeasurementRuntimePaths.from_root(tmp_path),
    ).read_events()
    assert events[0]["state"] == "process_started"
    assert events[-1]["state"] == "process_completed"
    assert events[-1]["returncode"] == 0
    assert [event["cursor"] for event in events] == list(range(1, len(events) + 1))
    console_events = [event for event in events if event["type"] == "console"]
    assert {event["source"] for event in console_events} == {"stdout", "stderr"}


def test_managed_process_large_output_does_not_deadlock(tmp_path: Path) -> None:
    completed = _run_fake(
        tmp_path,
        "stdout-repeat:8000",
        "stderr-repeat:8000",
    )

    assert completed.returncode == 0
    assert len((tmp_path / "stdout.log").read_text(encoding="utf-8").splitlines()) == 8000
    assert len((tmp_path / "stderr.log").read_text(encoding="utf-8").splitlines()) == 8000


def test_managed_process_times_out_and_preserves_live_diagnostics(tmp_path: Path) -> None:
    event_stream = MeasurementEventStream(
        "measurement-123",
        MeasurementRuntimePaths.from_root(tmp_path),
    )

    with pytest.raises(RuntimeError, match="orchestrator timed out after"):
        cms_bench._run_managed_process(
            _fake_command("stdout:SETUP START: blocked operation", "pause:5"),
            cwd=Path("."),
            stdout_path=tmp_path / "stdout.log",
            stderr_path=tmp_path / "stderr.log",
            event_stream=event_stream,
            timeout_s=2.0,
        )

    assert "SETUP START: blocked operation" in (tmp_path / "stdout.log").read_text(
        encoding="utf-8"
    )
    assert any(event.get("state") == "process_timed_out" for event in event_stream.read_events())


def test_managed_process_cancellation_stops_child_and_drains_output(tmp_path: Path) -> None:
    event_stream = MeasurementEventStream(
        "measurement-123",
        MeasurementRuntimePaths.from_root(tmp_path),
    )
    cancel_event = threading.Event()
    outcomes: list[subprocess.CompletedProcess] = []
    worker = threading.Thread(
        target=lambda: outcomes.append(
            cms_bench._run_managed_process(
                _fake_command("stdout:before-cancel", "pause:30"),
                cwd=Path("."),
                stdout_path=tmp_path / "stdout.log",
                stderr_path=tmp_path / "stderr.log",
                event_stream=event_stream,
                cancel_event=cancel_event,
            )
        ),
        daemon=True,
    )
    worker.start()

    stdout_path = tmp_path / "stdout.log"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if stdout_path.exists() and "before-cancel" in stdout_path.read_text(encoding="utf-8"):
            break
        time.sleep(0.01)
    cancel_event.set()
    worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert outcomes[0].returncode != 0
    assert stdout_path.read_text(encoding="utf-8") == "before-cancel\n"
    states = [event.get("state") for event in event_stream.read_events()]
    assert "process_cancellation_requested" in states
    assert states[-1] == "process_cancelled"


def test_linux_process_tree_termination_escalates_process_group(monkeypatch) -> None:
    sent_signals = []
    sigkill = getattr(signal, "SIGKILL", 9)

    class FakeProcess:
        pid = 1234

        def __init__(self) -> None:
            self.wait_count = 0

        def wait(self, timeout=None):
            self.wait_count += 1
            if self.wait_count == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return -9

    monkeypatch.setattr(cms_bench.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cms_bench.signal, "SIGKILL", sigkill, raising=False)
    monkeypatch.setattr(
        cms_bench.os,
        "killpg",
        lambda pid, sent_signal: sent_signals.append((pid, sent_signal)),
        raising=False,
    )

    cms_bench._terminate_process_tree(FakeProcess())

    assert sent_signals == [
        (1234, signal.SIGTERM),
        (1234, sigkill),
    ]


@pytest.mark.parametrize(
    ("actions", "expected_returncode"),
    [
        (("stdout:success",), 0),
        (("stderr:failure", "exit:7"), 7),
        (("stdout:before-abrupt-exit", "abrupt-exit:9"), 9),
    ],
)
def test_managed_process_preserves_child_exit_status_and_final_output(
    tmp_path: Path,
    actions: tuple[str, ...],
    expected_returncode: int,
) -> None:
    completed = _run_fake(tmp_path, *actions)

    assert completed.returncode == expected_returncode
    if "abrupt-exit:9" in actions:
        assert (tmp_path / "stdout.log").read_text(encoding="utf-8") == "before-abrupt-exit\n"


def test_orchestrator_starts_python_unbuffered_and_uses_runtime_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = MeasurementIdentity(
        measurement_id="measurement-123",
        run_name="Streaming test",
        created_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    firmware = FirmwareData(
        dut_img=FirmwareImage(name="dut.s37", url=""),
        tester_img=FirmwareImage(name="tester.s37", url=""),
    )
    request = MeasurementRequest(
        request_id=identity.measurement_id,
        selector=SuiteSelector(mode="ble", feature="beacon", config="default"),
        firmware=firmware,
        identity=identity,
    )
    session = BenchSession(
        measurement_id=identity.measurement_id,
        runtime_dir=tmp_path,
        manifest=firmware,
        request=request,
    )
    observed: dict[str, object] = {}

    def fake_managed_process(
        command,
        *,
        cwd,
        stdout_path,
        stderr_path,
        event_stream,
        log_max_bytes,
        timeout_s,
        cancel_event,
    ):
        observed.update(
            command=command,
            cwd=cwd,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            event_stream=event_stream,
            log_max_bytes=log_max_bytes,
            timeout_s=timeout_s,
            cancel_event=cancel_event,
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(cms_bench, "_run_managed_process", fake_managed_process)

    completed = cms_bench._run_test_orchestrator(
        session,
        tmp_path / "orchestrator_results.jsonl",
        tmp_path / "cms_fw_metadata.json",
    )

    command = observed["command"]
    assert isinstance(command, list)
    assert command[:3] == [sys.executable, "-u", "test_orchestrator.py"]
    assert observed["stdout_path"] == session.runtime_paths.stdout
    assert observed["stderr_path"] == session.runtime_paths.stderr
    assert observed["event_stream"] is session.event_stream
    assert observed["log_max_bytes"] == session.max_console_log_bytes
    assert observed["timeout_s"] == session.execution_timeout_s
    assert observed["cancel_event"] is session.cancel_event
    assert completed.returncode == 0

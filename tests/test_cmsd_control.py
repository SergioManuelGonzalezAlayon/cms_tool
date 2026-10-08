from __future__ import annotations

import argparse
import errno
import json
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cmsd
from cmsd import (
    DaemonConfig,
    daemon_config_from_args,
    daemon_control_paths,
    daemon_pid_is_alive,
    daemon_pid_state,
    read_daemon_pid_record,
)


def _config(tmp_path: Path) -> DaemonConfig:
    return DaemonConfig(
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "artifacts",
        bench_config=tmp_path / "cms_bench_runtime.json",
    )


def test_control_paths_are_derived_from_artifact_root(tmp_path: Path) -> None:
    paths = daemon_control_paths(_config(tmp_path))

    assert paths.root == tmp_path / "artifacts" / "control"
    assert paths.pid == paths.root / "cmsd.pid"
    assert paths.log == paths.root / "cmsd.log"
    assert paths.status == paths.root / "cmsd.status.json"
    assert paths.lifecycle == paths.root / "cmsd.lifecycle.jsonl"


def test_pid_record_is_validated_before_it_is_used(tmp_path: Path) -> None:
    pid_path = tmp_path / "cmsd.pid"
    pid_path.write_text('{"pid": 42, "command": "serve"}\n', encoding="utf-8")

    assert read_daemon_pid_record(pid_path) == {"pid": 42, "command": "serve"}

    pid_path.write_text('{"pid": "42"}\n', encoding="utf-8")
    assert read_daemon_pid_record(pid_path) is None


def test_pid_state_distinguishes_missing_invalid_and_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_path = tmp_path / "cmsd.pid"
    assert daemon_pid_state(pid_path) == "missing"

    pid_path.write_text("not json\n", encoding="utf-8")
    assert daemon_pid_state(pid_path) == "invalid"

    pid_path.write_text('{"pid": 42}\n', encoding="utf-8")
    monkeypatch.setattr(cmsd, "daemon_pid_is_alive", lambda _pid: False)
    assert daemon_pid_state(pid_path) == "stale"


def test_pid_liveness_checks_process_existence_without_signaling(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, signal: int) -> None:
        calls.append((pid, signal))

    monkeypatch.setattr(cmsd.os, "kill", fake_kill)
    assert daemon_pid_is_alive(42) is True
    assert calls == [(42, 0)]

    def missing_process(_pid: int, _signal: int) -> None:
        raise OSError(errno.ESRCH, "no such process")

    monkeypatch.setattr(cmsd.os, "kill", missing_process)
    assert daemon_pid_is_alive(42) is False
    assert daemon_pid_is_alive(0) is False


def test_parser_defaults_to_foreground_serve() -> None:
    args = cmsd.build_daemon_parser().parse_args([])

    assert args.command == "serve"


def test_parser_accepts_detached_start() -> None:
    args = cmsd.build_daemon_parser().parse_args(["start"])

    assert args.command == "start"


def test_parser_accepts_status() -> None:
    args = cmsd.build_daemon_parser().parse_args(["status"])

    assert args.command == "status"


def test_parser_accepts_stop() -> None:
    args = cmsd.build_daemon_parser().parse_args(["stop"])

    assert args.command == "stop"


def test_parser_accepts_restart() -> None:
    args = cmsd.build_daemon_parser().parse_args(["restart"])

    assert args.command == "restart"


def test_parser_maps_timeout_overrides_to_daemon_config() -> None:
    args = cmsd.build_daemon_parser().parse_args(
        ["--startup-timeout", "12.5", "--stop-timeout", "3.0", "status"]
    )

    config = cmsd.daemon_config_from_args(args)

    assert config.startup_timeout_s == 12.5
    assert config.stop_timeout_s == 3.0


def test_config_from_args_overrides_only_explicit_values(tmp_path: Path) -> None:
    args = argparse.Namespace(
        host="127.0.0.1",
        port=8123,
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "artifacts",
        bench_config=tmp_path / "bench.json",
    )

    config = daemon_config_from_args(args)

    assert config.host == "127.0.0.1"
    assert config.port == 8123
    assert config.runtime_root == tmp_path / "runtime"
    assert config.artifacts_root == tmp_path / "artifacts"
    assert config.bench_config == tmp_path / "bench.json"


def test_status_reports_stopped_without_a_pid_file(tmp_path: Path) -> None:
    status = cmsd.get_daemon_status(_config(tmp_path))

    assert status["state"] == "stopped"
    assert status["recorded_pid"] is None
    assert status["process_alive"] is False
    assert status["health_responding"] is False
    assert status["daemon_initialized"] is None


def test_status_reports_starting_when_process_is_alive_but_health_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    pid_path = daemon_control_paths(config).pid
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text(
        json.dumps(
            {
                "pid": 12345,
                "command": "serve",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cmsd, "daemon_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(cmsd, "_daemon_json_request", lambda _config, _path: (False, None))

    status = cmsd.get_daemon_status(config)

    assert status["state"] == "starting"
    assert status["recorded_pid"] == 12345
    assert status["process_alive"] is True
    assert status["health_responding"] is False


def test_status_reports_running_and_includes_service_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    paths = daemon_control_paths(config)
    paths.pid.parent.mkdir(parents=True)
    paths.pid.write_text(
        json.dumps(
            {
                "pid": 12345,
                "command": "serve",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "log_path": str(paths.log),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cmsd, "daemon_pid_is_alive", lambda _pid: True)

    def fake_request(_config: DaemonConfig, path: str) -> tuple[bool, dict[str, object]]:
        if path == "/health":
            return True, {"state": "idle"}
        return True, {
            "daemon_initialized": True,
            "bench_state": "idle",
            "active_measurement_id": "measurement-123",
            "startup_error": "",
        }

    monkeypatch.setattr(cmsd, "_daemon_json_request", fake_request)

    status = cmsd.get_daemon_status(config)

    assert status["state"] == "running"
    assert status["health_responding"] is True
    assert status["daemon_initialized"] is True
    assert status["bench_state"] == "idle"
    assert status["active_measurement_id"] == "measurement-123"
    assert status["host"] == config.host
    assert status["port"] == config.port
    assert status["log_path"] == str(paths.log)


def test_status_reports_running_unhealthy_after_startup_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = DaemonConfig(
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "artifacts",
        bench_config=tmp_path / "cms_bench_runtime.json",
        startup_timeout_s=1,
    )
    pid_path = daemon_control_paths(config).pid
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text(
        json.dumps(
            {
                "pid": 12345,
                "command": "serve",
                "started_at": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cmsd, "daemon_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(cmsd, "_daemon_json_request", lambda _config, _path: (False, None))

    status = cmsd.get_daemon_status(config)

    assert status["state"] == "running_unhealthy"


def test_status_reports_stale_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    pid_path = daemon_control_paths(config).pid
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text('{"pid": 12345, "command": "serve"}\n', encoding="utf-8")
    monkeypatch.setattr(cmsd, "daemon_pid_is_alive", lambda _pid: False)

    status = cmsd.get_daemon_status(config)

    assert status["state"] == "stale_pid"
    assert status["recorded_pid"] == 12345
    assert status["process_alive"] is False


def test_daemon_status_snapshot_is_written_atomically(tmp_path: Path) -> None:
    config = _config(tmp_path)
    daemon = cmsd.CMSDaemon(config)
    daemon._status_persistence_enabled = True
    daemon.initialized = True
    daemon.active_measurement_id = "measurement-123"
    daemon.startup_error = ""

    daemon._write_status_snapshot("running")

    paths = daemon_control_paths(config)
    snapshot = json.loads(paths.status.read_text(encoding="utf-8"))
    assert snapshot["state"] == "running"
    assert snapshot["pid"] == os.getpid()
    assert snapshot["command"] == "serve"
    assert snapshot["daemon_initialized"] is True
    assert snapshot["active_measurement_id"] == "measurement-123"
    assert snapshot["status_file"] == str(paths.status)
    assert list(paths.root.glob(f".{paths.status.name}.*.tmp")) == []


def test_status_uses_persisted_snapshot_when_service_is_unreachable(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = daemon_control_paths(config)
    paths.status.parent.mkdir(parents=True)
    paths.status.write_text(
        json.dumps(
            {
                "state": "running_unhealthy",
                "daemon_initialized": False,
                "bench_state": "error",
                "active_measurement_id": "measurement-123",
                "startup_error": "bench configuration is invalid",
                "updated_at": "2026-10-07T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    status = cmsd.get_daemon_status(config)

    assert status["state"] == "stopped"
    assert status["daemon_initialized"] is False
    assert status["bench_state"] == "error"
    assert status["active_measurement_id"] == "measurement-123"
    assert status["startup_error"] == "bench configuration is invalid"
    assert status["status_updated_at"] == "2026-10-07T12:00:00+00:00"


def test_status_records_unexpected_child_exit_for_a_stale_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    paths = daemon_control_paths(config)
    paths.pid.parent.mkdir(parents=True)
    paths.pid.write_text('{"pid": 12345, "command": "serve"}\n', encoding="utf-8")
    paths.status.write_text(
        json.dumps(
            {
                "pid": 12345,
                "state": "running",
                "daemon_initialized": True,
                "updated_at": "2026-10-07T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cmsd, "daemon_pid_is_alive", lambda _pid: False)

    status = cmsd.get_daemon_status(config)

    assert status["state"] == "stale_pid"
    assert status["exit_reason"] == "process_not_alive"
    persisted = json.loads(paths.status.read_text(encoding="utf-8"))
    assert persisted["state"] == "child_exit"
    assert persisted["child_exit_detected_at"]


def test_stop_reports_already_stopped_without_a_pid_file(tmp_path: Path) -> None:
    config = _config(tmp_path)

    result = cmsd.stop_daemon(config)

    assert result["state"] == "stopped"
    assert result["already_stopped"] is True
    assert result["pid"] is None
    lifecycle = daemon_control_paths(config).lifecycle.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lifecycle] == ["stop_requested", "stopped"]


def test_stop_removes_a_stale_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    pid_path = daemon_control_paths(config).pid
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text('{"pid": 12345, "command": "serve"}\n', encoding="utf-8")
    monkeypatch.setattr(cmsd, "daemon_pid_is_alive", lambda _pid: False)

    result = cmsd.stop_daemon(config)

    assert result["already_stopped"] is True
    assert result["pid"] == 12345
    assert not pid_path.exists()


def test_stop_sends_sigterm_waits_for_exit_and_removes_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    pid_path = daemon_control_paths(config).pid
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text('{"pid": 12345, "command": "serve"}\n', encoding="utf-8")
    alive_results = iter([True, True, False])
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(cmsd, "daemon_pid_is_alive", lambda _pid: next(alive_results))
    monkeypatch.setattr(cmsd, "_daemon_process_matches_record", lambda _record: True)
    monkeypatch.setattr(cmsd.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    result = cmsd.stop_daemon(config)

    assert result["already_stopped"] is False
    assert result["pid"] == 12345
    assert signals == [(12345, signal.SIGTERM)]
    assert not pid_path.exists()


def test_stop_preserves_pid_file_when_process_does_not_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    pid_path = daemon_control_paths(config).pid
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text('{"pid": 12345, "command": "serve"}\n', encoding="utf-8")
    monkeypatch.setattr(cmsd, "daemon_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(cmsd, "_daemon_process_matches_record", lambda _record: True)
    monkeypatch.setattr(cmsd.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(cmsd, "wait_for_daemon_exit", lambda _pid, _timeout: False)

    with pytest.raises(RuntimeError, match="did not exit"):
        cmsd.stop_daemon(config)

    assert pid_path.exists()
    lifecycle = daemon_control_paths(config).lifecycle.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lifecycle] == ["stop_requested", "stop_failed"]


def test_restart_stops_before_starting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    calls: list[str] = []
    stop_result = {"state": "stopped", "already_stopped": True, "pid": None}
    start_result = {"pid": 54321, "log_path": str(tmp_path / "cmsd.log")}

    def fake_stop(_config: DaemonConfig) -> dict[str, object]:
        calls.append("stop")
        return stop_result

    def fake_start(_config: DaemonConfig) -> dict[str, object]:
        calls.append("start")
        return start_result

    monkeypatch.setattr(cmsd, "stop_daemon", fake_stop)
    monkeypatch.setattr(cmsd, "start_daemon", fake_start)

    result = cmsd.restart_daemon(config)

    assert calls == ["stop", "start"]
    assert result == {"stop": stop_result, "start": start_result}
    lifecycle = daemon_control_paths(config).lifecycle.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lifecycle] == ["restart", "restart"]
    assert json.loads(lifecycle[0])["phase"] == "requested"
    assert json.loads(lifecycle[1])["phase"] == "completed"


def test_restart_does_not_start_when_stop_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    calls: list[str] = []

    def fake_stop(_config: DaemonConfig) -> dict[str, object]:
        calls.append("stop")
        raise RuntimeError("stop failed")

    def fake_start(_config: DaemonConfig) -> dict[str, object]:
        calls.append("start")
        return {"pid": 54321}

    monkeypatch.setattr(cmsd, "stop_daemon", fake_stop)
    monkeypatch.setattr(cmsd, "start_daemon", fake_start)

    with pytest.raises(RuntimeError, match="stop failed"):
        cmsd.restart_daemon(config)

    assert calls == ["stop"]
    lifecycle = daemon_control_paths(config).lifecycle.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lifecycle] == ["restart", "restart"]
    assert json.loads(lifecycle[1])["phase"] == "failed"


def test_restart_preserves_runtime_and_persistent_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    result_artifact = config.artifacts_root / "measurements" / "measurement-123" / "result.json"
    staged_firmware = config.artifacts_root / "staged" / "dut.s37"
    runtime_status = config.runtime_root / "measurements" / "measurement-123" / "current_status.json"
    for path, contents in (
        (result_artifact, "persistent result"),
        (staged_firmware, "staged firmware"),
        (runtime_status, "active runtime"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    monkeypatch.setattr(
        cmsd,
        "start_daemon",
        lambda _config: {"pid": 54321, "log_path": str(daemon_control_paths(config).log)},
    )

    cmsd.restart_daemon(config)

    assert result_artifact.read_text(encoding="utf-8") == "persistent result"
    assert staged_firmware.read_text(encoding="utf-8") == "staged firmware"
    assert runtime_status.read_text(encoding="utf-8") == "active runtime"


def test_start_detaches_child_redirects_output_and_writes_pid_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345

        def terminate(self) -> None:
            captured["terminated"] = True

        def wait(self, timeout: float) -> None:
            captured["wait_timeout"] = timeout

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(cmsd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cmsd, "wait_for_daemon_health", lambda _process, _config: None)

    record = cmsd.start_daemon(config)
    pid_path = daemon_control_paths(config).pid
    stored_record = json.loads(pid_path.read_text(encoding="utf-8"))
    kwargs = captured["kwargs"]

    assert record["pid"] == 12345
    assert stored_record == record
    assert captured["command"][-1] == "serve"
    assert kwargs["stdin"] is cmsd.subprocess.DEVNULL
    assert kwargs["stderr"] is cmsd.subprocess.STDOUT
    assert kwargs["close_fds"] is True
    if os.name == "nt":
        assert "creationflags" in kwargs
    else:
        assert kwargs["start_new_session"] is True
    lifecycle = daemon_control_paths(config).lifecycle.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lifecycle] == ["start_requested", "started"]


def test_start_records_failure_when_child_cannot_be_spawned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)

    def fake_popen(_command: list[str], **_kwargs: object) -> object:
        raise OSError("spawn failed")

    monkeypatch.setattr(cmsd.subprocess, "Popen", fake_popen)

    with pytest.raises(OSError, match="spawn failed"):
        cmsd.start_daemon(config)

    lifecycle = daemon_control_paths(config).lifecycle.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lifecycle] == ["start_requested", "start_failed"]
    assert json.loads(lifecycle[1])["error"] == "spawn failed"
    status = json.loads(daemon_control_paths(config).status.read_text(encoding="utf-8"))
    assert status["state"] == "start_failed"
    assert status["startup_error"] == "spawn failed"
    assert status["exit_reason"] == "startup_failed"


def test_start_rejects_running_daemon_without_removing_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    paths = daemon_control_paths(config)
    paths.pid.parent.mkdir(parents=True)
    paths.pid.write_text(
        json.dumps({"pid": 12345, "command": "serve"}) + "\n",
        encoding="utf-8",
    )
    paths.status.write_text(
        json.dumps({"pid": 12345, "state": "running"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cmsd, "daemon_pid_is_alive", lambda _pid: True)

    with pytest.raises(RuntimeError, match="already running"):
        cmsd.start_daemon(config)

    assert paths.pid.exists()
    assert json.loads(paths.status.read_text(encoding="utf-8"))["state"] == "running"
    lifecycle = paths.lifecycle.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lifecycle] == ["start_requested", "start_failed"]


def test_wait_for_daemon_health_reports_early_child_exit(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class ExitedProcess:
        def poll(self) -> int:
            return 17

    with pytest.raises(RuntimeError, match="returncode=17"):
        cmsd.wait_for_daemon_health(ExitedProcess(), config)


def test_wait_for_daemon_health_reports_startup_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = DaemonConfig(
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "artifacts",
        bench_config=tmp_path / "cms_bench_runtime.json",
        startup_timeout_s=0.1,
    )

    class RunningProcess:
        def poll(self) -> None:
            return None

    class FakeClock:
        def __init__(self) -> None:
            self.values = iter([100.0, 100.2])

        def monotonic(self) -> float:
            return next(self.values)

        def sleep(self, _seconds: float) -> None:
            raise AssertionError("startup timeout test should not sleep")

    monkeypatch.setattr(cmsd, "time", FakeClock())
    monkeypatch.setattr(cmsd, "_daemon_health_ready", lambda _config: False)

    with pytest.raises(RuntimeError, match="health check timed out"):
        cmsd.wait_for_daemon_health(RunningProcess(), config)

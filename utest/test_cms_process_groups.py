from __future__ import annotations

import signal
from pathlib import Path

import pytest

import cms_system
from cms_bench import BenchResources, CMSBench, MeasurementEventStream, MeasurementRuntimePaths
from cms_protocol import (
    BenchIdentity,
    BenchState,
    FirmwareData,
    FirmwareImage,
    MeasurementRequest,
    MeasurementState,
    SuiteSelector,
)
from cmsd import CMSDaemon, DaemonConfig


def _write_proc_stat(
    proc_root: Path,
    pid: int,
    *,
    comm: str,
    state: str,
    process_group_id: int,
) -> None:
    process_dir = proc_root / str(pid)
    process_dir.mkdir(parents=True)
    # The comm field may contain ')' characters; the parser must use its last delimiter.
    (process_dir / "stat").write_text(
        f"{pid} ({comm}) {state} 1 {process_group_id} 1 0 0\n",
        encoding="utf-8",
    )


def _daemon(tmp_path: Path) -> CMSDaemon:
    return CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )


def _record_process_group(runtime_dir: Path, measurement_id: str, process_group_id: int) -> None:
    MeasurementEventStream(
        measurement_id,
        MeasurementRuntimePaths.from_root(runtime_dir),
    ).append_lifecycle(
        "process_started",
        pid=process_group_id,
        process_group_id=process_group_id,
        command=["commander-cli", "adapter", "dbgmode", "MCU"],
    )


def test_process_group_scan_ignores_zombies_and_handles_parentheses_in_comm(tmp_path: Path) -> None:
    _write_proc_stat(
        tmp_path,
        101,
        comm="commander-cli (hung)",
        state="S",
        process_group_id=55,
    )
    _write_proc_stat(tmp_path, 102, comm="zombie", state="Z", process_group_id=55)
    _write_proc_stat(tmp_path, 103, comm="other", state="R", process_group_id=77)

    assert cms_system.active_process_group_pids(55, proc_root=tmp_path) == (101,)


def test_normal_process_group_completion_releases_bench_only_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _daemon(tmp_path)
    daemon.bench = CMSBench()
    daemon.bench.state = BenchState.TESTING
    runtime_dir = daemon.create_runtime_workspace("measurement-clean")
    _record_process_group(runtime_dir, "measurement-clean", 321)
    monkeypatch.setattr(cms_system, "active_process_group_pids", lambda _group_id: ())

    def fail_if_signalled(*_args):
        pytest.fail("a clean process group should not be signalled")

    monkeypatch.setattr(cms_system.os, "killpg", fail_if_signalled, raising=False)

    daemon._verify_measurement_process_groups("measurement-clean", runtime_dir)

    assert daemon.bench.state is BenchState.IDLE
    assert daemon.bench.cleanup_error == ""


def test_orphaned_commander_member_is_stopped_before_bench_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _daemon(tmp_path)
    daemon.bench = CMSBench()
    daemon.bench.state = BenchState.TESTING
    runtime_dir = daemon.create_runtime_workspace("measurement-commander-orphan")
    _record_process_group(runtime_dir, "measurement-commander-orphan", 654)
    active_members = iter(((9876,), ()))
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        cms_system,
        "active_process_group_pids",
        lambda _group_id: next(active_members),
    )
    monkeypatch.setattr(
        cms_system.os,
        "killpg",
        lambda group_id, sent_signal: signals.append((group_id, sent_signal)),
        raising=False,
    )

    daemon._verify_measurement_process_groups(
        "measurement-commander-orphan",
        runtime_dir,
    )

    assert signals == [(654, signal.SIGTERM)]
    assert daemon.bench.state is BenchState.IDLE
    records = MeasurementEventStream(
        "measurement-commander-orphan",
        MeasurementRuntimePaths.from_root(runtime_dir),
    ).read_events()
    cleanup = next(record for record in records if record.get("state") == "process_groups_verified")
    assert cleanup["terminated_pids"] == {"654": [9876]}


def test_cleanup_failure_keeps_bench_error_and_blocks_next_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _daemon(tmp_path)
    daemon.bench = CMSBench()
    daemon.bench.state = BenchState.TESTING
    runtime_dir = daemon.create_runtime_workspace("measurement-leaked")
    _record_process_group(runtime_dir, "measurement-leaked", 654)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        cms_system,
        "active_process_group_pids",
        lambda _group_id: (9876,),
    )
    monkeypatch.setattr(
        cms_system.os,
        "killpg",
        lambda group_id, sent_signal: signals.append((group_id, sent_signal)),
        raising=False,
    )
    monkeypatch.setattr(cms_system, "PROCESS_GROUP_TERMINATE_TIMEOUT_S", 0.0)
    monkeypatch.setattr(cms_system, "PROCESS_GROUP_KILL_TIMEOUT_S", 0.0)

    with pytest.raises(RuntimeError, match=r"still has active process\(es\) after SIGKILL"):
        daemon._verify_measurement_process_groups("measurement-leaked", runtime_dir)

    assert signals == [(654, signal.SIGTERM), (654, getattr(signal, "SIGKILL", 9))]
    assert daemon.bench.state is BenchState.ERROR
    assert "process cleanup failed" in daemon.bench.cleanup_error
    daemon.bench.identity = BenchIdentity(bench_id="cleanup-failed-test")
    daemon.bench.resources = BenchResources()
    daemon.bench.initialized = True
    health = daemon.bench.health_check()
    assert any(check.name == "process_cleanup" for check in health.checks)

    blocked_request = MeasurementRequest(
        request_id="measurement-after-cleanup-failure",
        selector=SuiteSelector(mode="ble", feature="beacon"),
        firmware=FirmwareData(
            dut_img=FirmwareImage(name="dut.s37", url=""),
            tester_img=FirmwareImage(name="tester.s37", url=""),
        ),
    )
    result = daemon.execute_measurement(blocked_request, identity_assigned=True)

    assert result.state is MeasurementState.FAILED
    assert "measurement execution is blocked" in result.message
    assert daemon.active_measurement_id is None


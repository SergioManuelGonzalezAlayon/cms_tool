from pathlib import Path

import pytest

from cms_bench import MeasurementRuntimePaths
from cms_protocol import FirmwareData, FirmwareImage, MeasurementRequest, SuiteSelector
from cmsd import CMSDaemon, DaemonConfig


def _request(measurement_id: str) -> MeasurementRequest:
    return MeasurementRequest(
        request_id=measurement_id,
        selector=SuiteSelector(mode="ble", feature="beacon", config="default"),
        firmware=FirmwareData(
            dut_img=FirmwareImage(name="dut.s37", url=""),
            tester_img=FirmwareImage(name="tester.s37", url=""),
        ),
    )


def test_measurement_workspace_owns_all_active_paths(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    artifacts_root = tmp_path / "artifacts"
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=runtime_root,
            artifacts_root=artifacts_root,
            bench_config=tmp_path / "bench.json",
        )
    )
    measurement_id = "measurement-123"

    runtime_dir = daemon.create_runtime_workspace(measurement_id)
    session = daemon.build_session(_request(measurement_id), runtime_dir)

    assert runtime_dir == runtime_root / "measurements" / measurement_id
    assert session.runtime_paths == MeasurementRuntimePaths(
        root=runtime_dir,
        status=runtime_dir / "current_status.json",
        events=runtime_dir / "events.jsonl",
        console_events=runtime_dir / "console_events",
        stdout=runtime_dir / "test_stdout.log",
        stderr=runtime_dir / "test_stderr.log",
    )
    for active_path in session.runtime_paths.active_paths():
        assert active_path.is_relative_to(runtime_dir)
        assert not active_path.is_relative_to(artifacts_root)


def test_daemon_propagates_execution_timeout_to_bench_session(tmp_path: Path) -> None:
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
            execution_timeout_s=123.0,
        )
    )
    measurement_id = "measurement-timeout"

    session = daemon.build_session(
        _request(measurement_id),
        daemon.create_runtime_workspace(measurement_id),
    )

    assert session.execution_timeout_s == 123.0


def test_daemon_reads_execution_timeout_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CMSD_EXECUTION_TIMEOUT_S", "456")

    daemon_config = DaemonConfig(
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "artifacts",
        bench_config=tmp_path / "bench.json",
    )

    assert daemon_config.execution_timeout_s == 456.0


def test_abort_signals_active_session_without_publishing_terminal_state(tmp_path: Path) -> None:
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )
    request = _request("measurement-cancel")
    daemon.initialize()
    daemon.measurement_accept(request)

    result = daemon.measurement_abort(request.request_id)

    assert daemon.cancellation_events[request.request_id].is_set()
    assert daemon.active_measurement_id == request.request_id
    assert result.state.value == "accepted"
    assert result.message == "cancellation requested"


def test_runtime_workspace_rejects_measurement_id_path_traversal(tmp_path: Path) -> None:
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )

    with pytest.raises(ValueError, match="measurement_id"):
        daemon.create_runtime_workspace("../outside-runtime-root")


def test_build_session_rejects_non_authoritative_runtime_directory(tmp_path: Path) -> None:
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )
    measurement_id = "measurement-123"

    with pytest.raises(ValueError, match="authoritative runtime workspace"):
        daemon.build_session(_request(measurement_id), tmp_path / "other")

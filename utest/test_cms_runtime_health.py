from collections import namedtuple
from pathlib import Path

import cmsd
import pytest
from cms_client import CMSClient
from cms_protocol import (
    BenchCheckResult,
    BenchCheckStatus,
    BenchHealthReport,
    BenchState,
    bench_health_report_to_dict,
)
from cmsd import CMSDaemon, DaemonConfig


_DiskUsage = namedtuple("DiskUsage", "total used free")


# pytest's monkeypatch fixture replaces host-dependent functions for one test
# and automatically restores the real functions when that test finishes.
def _fixed_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cmsd.shutil,
        "disk_usage",
        lambda _path: _DiskUsage(total=4096, used=1024, free=3072),
    )


def test_mountinfo_uses_the_most_specific_mount() -> None:
    mountinfo = "\n".join(
        [
            "20 1 8:1 / / rw,relatime - ext4 /dev/root rw",
            "21 20 0:18 / /run rw,nosuid,nodev - tmpfs tmpfs rw",
        ]
    )

    assert cmsd._filesystem_type_from_mountinfo("/run/cmsd/measurements", mountinfo) == "tmpfs"


def test_runtime_filesystem_health_passes_for_expected_linux_ram_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_capacity(monkeypatch)
    monkeypatch.setattr(cmsd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cmsd, "_linux_filesystem_type", lambda _path: "tmpfs")

    check = cmsd._runtime_filesystem_check(tmp_path, ("tmpfs", "ramfs"))

    assert check.name == "runtime_filesystem"
    assert check.status is BenchCheckStatus.PASS
    assert check.summary == "runtime filesystem is tmpfs"
    assert "available_bytes=3072" in check.detail
    assert "total_bytes=4096" in check.detail


def test_runtime_filesystem_health_fails_for_persistent_linux_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_capacity(monkeypatch)
    monkeypatch.setattr(cmsd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cmsd, "_linux_filesystem_type", lambda _path: "ext4")

    check = cmsd._runtime_filesystem_check(tmp_path, ("tmpfs", "ramfs"))

    assert check.status is BenchCheckStatus.FAIL
    assert check.summary == "runtime filesystem ext4 is not RAM-backed"
    assert "expected=tmpfs,ramfs" in check.detail
    assert "available_bytes=3072" in check.detail


def test_runtime_filesystem_health_warns_when_platform_cannot_verify_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_capacity(monkeypatch)
    monkeypatch.setattr(cmsd.platform, "system", lambda: "Windows")

    check = cmsd._runtime_filesystem_check(tmp_path, ("tmpfs", "ramfs"))

    assert check.status is BenchCheckStatus.WARN
    assert check.summary == "runtime filesystem verification is unsupported on Windows"
    assert "available_bytes=3072" in check.detail


def test_daemon_health_checks_include_configured_runtime_filesystem_expectation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_capacity(monkeypatch)
    monkeypatch.setattr(cmsd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cmsd, "_linux_filesystem_type", lambda _path: "ramfs")
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
            expected_runtime_filesystems=("ramfs",),
        )
    )

    checks = {check.name: check for check in daemon._daemon_checks()}

    assert checks["runtime_filesystem"].status is BenchCheckStatus.PASS
    assert "available_bytes=3072" in checks["runtime_filesystem"].detail


def test_cms_client_health_receives_runtime_filesystem_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon_report = BenchHealthReport(
        state=BenchState.IDLE,
        summary="bench is ready",
        checks=[
            BenchCheckResult(
                name="runtime_filesystem",
                status=BenchCheckStatus.PASS,
                summary="runtime filesystem is tmpfs",
                detail="available_bytes=3072; total_bytes=4096",
            )
        ],
    )
    client = CMSClient(
        base_url="http://cms-bench:8000",
        config_path=tmp_path / "missing-client-config.json",
    )
    requests: list[tuple[str, str]] = []

    def fake_request(method: str, path: str) -> dict:
        requests.append((method, path))
        return bench_health_report_to_dict(daemon_report)

    monkeypatch.setattr(client, "_request_json", fake_request)

    client_report = client.get_health()

    assert requests == [("GET", "/health")]
    runtime_check = next(
        check for check in client_report.checks if check.name == "runtime_filesystem"
    )
    assert runtime_check.status is BenchCheckStatus.PASS
    assert runtime_check.summary == "runtime filesystem is tmpfs"
    assert runtime_check.detail == "available_bytes=3072; total_bytes=4096"

import base64
import hashlib
import json
from pathlib import Path

import pytest

from cms_protocol import (
    DifferentialFirmwareData,
    FirmwareData,
    FirmwareImage,
    FirmwareRoleImage,
    MeasurementRequest,
    SuiteSelector,
)
from cmsd import CMSDaemon, DaemonConfig, _write_differential_firmware_payloads


def _image(name: str, content: bytes) -> tuple[FirmwareImage, str]:
    return FirmwareImage(
        name=name,
        url=name,
        sha256=hashlib.sha256(content).hexdigest(),
    ), base64.b64encode(content).decode("ascii")


def _request_and_payloads() -> tuple[MeasurementRequest, dict[str, str]]:
    payloads: dict[str, str] = {}

    def variant(name: str) -> FirmwareData:
        dut, dut_payload = _image(f"{name}__dut.s37", f"{name}-dut".encode())
        tester, tester_payload = _image(f"{name}__tester.s37", f"{name}-tester".encode())
        xapi, xapi_payload = _image(f"{name}__sl_bt.xapi", f"{name}-xapi".encode())
        payloads[f"differential_firmware.{name}.role_images.ncp_ble_dut_dcdc"] = dut_payload
        payloads[f"differential_firmware.{name}.role_images.ncp_ble_tester"] = tester_payload
        payloads[f"differential_firmware.{name}.xapi_file"] = xapi_payload
        return FirmwareData(
            dut_img=dut,
            tester_img=tester,
            xapi_file=xapi,
            sisdk_version=f"{name}-sdk",
            sisdk_hash=f"{name}-hash",
            role_images=(
                FirmwareRoleImage("dut", "ncp_ble_dut_dcdc", dut),
                FirmwareRoleImage("tester", "ncp_ble_tester", tester),
            ),
        )

    version1 = variant("version1")
    version2 = variant("version2")
    return (
        MeasurementRequest(
            request_id="diff-stage-001",
            selector=SuiteSelector(mode="ble", feature="beacon"),
            firmware=version2,
            differential_firmware=DifferentialFirmwareData(version1=version1, version2=version2),
        ),
        payloads,
    )


def test_differential_payloads_stage_atomically_in_isolated_variant_directories(
    tmp_path: Path,
) -> None:
    request, payloads = _request_and_payloads()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    _write_differential_firmware_payloads(request, runtime_dir, payloads)

    root = runtime_dir / "firmware"
    assert (root / "version2" / "roles" / "dut" / "ncp_ble_dut_dcdc" / "version2__dut.s37").read_bytes() == b"version2-dut"
    assert (root / "version1" / "roles" / "tester" / "ncp_ble_tester" / "version1__tester.s37").read_bytes() == b"version1-tester"
    assert (root / "version2" / "xapi" / "version2__sl_bt.xapi").read_bytes() == b"version2-xapi"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["variants"]["version1"]["sisdk_version"] == "version1-sdk"
    assert not list(runtime_dir.glob(".firmware-staging-*"))


def test_differential_staging_failure_does_not_publish_a_partial_pair(tmp_path: Path) -> None:
    request, payloads = _request_and_payloads()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    payloads.pop("differential_firmware.version1.xapi_file")

    with pytest.raises(RuntimeError, match="version1.xapi_file"):
        _write_differential_firmware_payloads(request, runtime_dir, payloads)

    assert not (runtime_dir / "firmware").exists()
    assert not list(runtime_dir.glob(".firmware-staging-*"))


def test_validated_differential_staging_is_retained_by_request_id(tmp_path: Path) -> None:
    request, payloads = _request_and_payloads()
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime-root",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )
    runtime_dir = daemon.create_runtime_workspace(request.request_id)
    _write_differential_firmware_payloads(request, runtime_dir, payloads)

    artifact = daemon._retain_staged_differential_firmware(request, runtime_dir)
    retained = daemon.get_current_differential_firmware()

    assert retained is not None
    assert retained.request is request
    assert retained.runtime_dir == runtime_dir
    assert retained.manifest_path == runtime_dir / "firmware" / "manifest.json"
    assert artifact.path == retained.manifest_path
    assert artifact.name == "cmsd/diff-stage-001/firmware/manifest.json"


def test_differential_staging_is_not_retained_without_its_manifest(tmp_path: Path) -> None:
    request, _ = _request_and_payloads()
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime-root",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )
    runtime_dir = daemon.create_runtime_workspace(request.request_id)

    with pytest.raises(RuntimeError, match="staging manifest is missing"):
        daemon._retain_staged_differential_firmware(request, runtime_dir)

    assert daemon.get_current_differential_firmware() is None


def test_staging_replacement_retains_only_one_current_stage_per_kind(tmp_path: Path) -> None:
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime-root",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )
    normal_first, _ = _request_and_payloads()
    normal_second, _ = _request_and_payloads()
    normal_second.request_id = "normal-stage-002"
    normal_first_dir = daemon.create_runtime_workspace("normal-stage-001")
    normal_second_dir = daemon.create_runtime_workspace(normal_second.request_id)

    daemon._retain_staged_normal_firmware(normal_first, normal_first_dir)
    daemon._retain_staged_normal_firmware(normal_second, normal_second_dir)

    assert daemon.last_firmware_request is normal_second
    assert daemon.last_firmware_runtime_dir == normal_second_dir
    assert not normal_first_dir.exists()
    assert normal_second_dir.is_dir()

    diff_first, _ = _request_and_payloads()
    diff_second, _ = _request_and_payloads()
    diff_second.request_id = "diff-stage-002"
    diff_first_dir = daemon.create_runtime_workspace(diff_first.request_id)
    diff_second_dir = daemon.create_runtime_workspace(diff_second.request_id)
    for runtime_dir in (diff_first_dir, diff_second_dir):
        manifest = runtime_dir / "firmware" / "manifest.json"
        manifest.parent.mkdir()
        manifest.write_text("{}", encoding="utf-8")

    daemon._retain_staged_differential_firmware(diff_first, diff_first_dir)
    daemon._retain_staged_differential_firmware(diff_second, diff_second_dir)

    assert daemon.get_current_differential_firmware() is not None
    assert daemon.get_current_differential_firmware().request is diff_second
    assert not diff_first_dir.exists()
    assert diff_second_dir.is_dir()


def test_staged_view_reports_only_current_normal_and_differential_provenance(tmp_path: Path) -> None:
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime-root",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )
    normal_request, _ = _request_and_payloads()
    normal_request.firmware.board = "BRD4002A"
    normal_request.firmware.device = "EFR32MG22"
    normal_request.firmware.sisdk_version = "2026.12.0"
    normal_dir = daemon.create_runtime_workspace("normal-stage")
    daemon._retain_staged_normal_firmware(normal_request, normal_dir)

    differential_request, _ = _request_and_payloads()
    differential_request.differential_firmware.version1.board = "BRD4002A"
    differential_request.differential_firmware.version1.device = "EFR32MG22"
    differential_request.differential_firmware.version2.board = "BRD4002A"
    differential_request.differential_firmware.version2.device = "EFR32MG22"
    differential_dir = daemon.create_runtime_workspace(differential_request.request_id)
    manifest = differential_dir / "firmware" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text("{}", encoding="utf-8")
    daemon._retain_staged_differential_firmware(differential_request, differential_dir)

    payload = daemon.get_staged()
    endpoint = next(
        route.endpoint
        for route in daemon.create_app().routes
        if getattr(route, "path", "") == "/staged"
    )

    assert payload == endpoint()
    assert set(payload) == {"normal", "differential"}
    assert payload["normal"]["intended_use"] == "normal test execution"
    assert payload["normal"]["target"] == {"board": "BRD4002A", "device": "EFR32MG22"}
    assert payload["normal"]["sisdk"]["version"] == "2026.12.0"
    assert payload["differential"]["intended_use"] == "differential version1/version2 comparison"
    assert payload["differential"]["version1"]["firmware"]
    assert payload["differential"]["version2"]["firmware"]
    assert "staging_id" not in payload["differential"]


def test_failed_differential_stage_releases_its_candidate_without_replacing_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime-root",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )
    current_request, _ = _request_and_payloads()
    current_dir = daemon.create_runtime_workspace(current_request.request_id)
    manifest = current_dir / "firmware" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text("{}", encoding="utf-8")
    daemon._retain_staged_differential_firmware(current_request, current_dir)

    candidate_request, candidate_payloads = _request_and_payloads()
    candidate_request.request_id = "failed-diff-stage"
    candidate_payloads.pop("differential_firmware.version1.xapi_file")
    monkeypatch.setattr(daemon, "get_status", lambda: {})

    result = daemon.stage_firmware(candidate_request, candidate_payloads)

    assert result.state.value == "failed"
    assert daemon.get_current_differential_firmware() is not None
    assert current_dir.is_dir()
    assert not (daemon.config.runtime_root / candidate_request.request_id).exists()


def test_diff_test_uses_the_current_differential_stage_without_a_staging_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, payloads = _request_and_payloads()
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime-root",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )
    runtime_dir = daemon.create_runtime_workspace(request.request_id)
    _write_differential_firmware_payloads(request, runtime_dir, payloads)
    daemon._retain_staged_differential_firmware(request, runtime_dir)
    captured = {}

    class ImmediateThread:
        def __init__(self, *, target, args, kwargs, daemon):
            captured["target"] = target
            captured["args"] = args
            captured["kwargs"] = kwargs

        def start(self) -> None:
            return None

    monkeypatch.setattr("cmsd.threading.Thread", ImmediateThread)

    result = daemon.start_staged_test(
        mode="diff",
        feature="beacon",
    )

    assert result.state.value == "accepted"
    assert captured["kwargs"]["differential_staging"].runtime_dir == runtime_dir
    assert captured["kwargs"]["staged_runtime_dir"] == runtime_dir
    assert captured["args"][0].differential_firmware is not None
    plan = json.loads(
        (runtime_dir / "firmware" / "paired_default_plan.json").read_text(encoding="utf-8")
    )
    assert plan["scope"] == "feature_default"
    assert plan["selector"]["feature"] == "beacon"
    assert {job["feature"] for job in plan["jobs"]} == {"beacon"}
    assert plan["jobs"]


def test_diff_test_rejects_when_no_current_differential_stage_exists(tmp_path: Path) -> None:
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime-root",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )

    result = daemon.start_staged_test(
        mode="diff",
        feature="beacon",
    )

    assert result.state.value == "failed"
    assert "SEND_FW --diff" in result.message

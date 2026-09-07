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

    child = variant("child")
    parent = variant("parent")
    return (
        MeasurementRequest(
            request_id="diff-stage-001",
            selector=SuiteSelector(mode="ble", feature="beacon"),
            firmware=child,
            differential_firmware=DifferentialFirmwareData(child=child, parent=parent),
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
    assert (root / "child" / "roles" / "dut" / "ncp_ble_dut_dcdc" / "child__dut.s37").read_bytes() == b"child-dut"
    assert (root / "parent" / "roles" / "tester" / "ncp_ble_tester" / "parent__tester.s37").read_bytes() == b"parent-tester"
    assert (root / "child" / "xapi" / "child__sl_bt.xapi").read_bytes() == b"child-xapi"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["variants"]["parent"]["sisdk_version"] == "parent-sdk"
    assert not list(runtime_dir.glob(".firmware-staging-*"))


def test_differential_staging_failure_does_not_publish_a_partial_pair(tmp_path: Path) -> None:
    request, payloads = _request_and_payloads()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    payloads.pop("differential_firmware.parent.xapi_file")

    with pytest.raises(RuntimeError, match="parent.xapi_file"):
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
    retained = daemon.get_staged_firmware(request.request_id)

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

    assert daemon.get_staged_firmware(request.request_id) is None

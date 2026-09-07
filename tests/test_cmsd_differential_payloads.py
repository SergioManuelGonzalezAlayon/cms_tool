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
    return (
        FirmwareImage(name=name, url=name, sha256=hashlib.sha256(content).hexdigest()),
        base64.b64encode(content).decode("ascii"),
    )


def _differential_request() -> tuple[MeasurementRequest, dict[str, str]]:
    payloads: dict[str, str] = {}

    def variant(name: str) -> FirmwareData:
        dut, dut_payload = _image(f"{name}-dut.s37", f"{name}-dut".encode())
        tester, tester_payload = _image(f"{name}-tester.s37", f"{name}-tester".encode())
        xapi, xapi_payload = _image(f"{name}-sl_bt.xapi", f"{name}-xapi".encode())
        payloads[f"differential_firmware.{name}.role_images.ncp_ble_dut_dcdc"] = dut_payload
        payloads[f"differential_firmware.{name}.role_images.ncp_ble_tester"] = tester_payload
        payloads[f"differential_firmware.{name}.xapi_file"] = xapi_payload
        return FirmwareData(
            dut_img=dut,
            tester_img=tester,
            xapi_file=xapi,
            role_images=(
                FirmwareRoleImage("dut", "ncp_ble_dut_dcdc", dut),
                FirmwareRoleImage("tester", "ncp_ble_tester", tester),
            ),
        )

    child = variant("child")
    parent = variant("parent")
    return (
        MeasurementRequest(
            request_id="diff-payload-001",
            selector=SuiteSelector(mode="ble", feature="beacon"),
            firmware=child,
            differential_firmware=DifferentialFirmwareData(child=child, parent=parent),
        ),
        payloads,
    )


def test_differential_payloads_publish_only_a_complete_pair(tmp_path: Path) -> None:
    request, payloads = _differential_request()

    _write_differential_firmware_payloads(request, tmp_path, payloads)

    root = tmp_path / "firmware"
    assert (root / "child" / "roles" / "dut" / "ncp_ble_dut_dcdc" / "child-dut.s37").read_bytes() == b"child-dut"
    assert (root / "parent" / "xapi" / "parent-sl_bt.xapi").read_bytes() == b"parent-xapi"
    assert json.loads((root / "manifest.json").read_text(encoding="utf-8"))["variants"]["child"]["sisdk_version"] == ""
    assert not list(tmp_path.glob(".firmware-staging-*"))


def test_differential_payload_failure_does_not_publish_a_partial_pair(tmp_path: Path) -> None:
    request, payloads = _differential_request()
    payloads.pop("differential_firmware.parent.xapi_file")

    with pytest.raises(RuntimeError, match="parent.xapi_file"):
        _write_differential_firmware_payloads(request, tmp_path, payloads)

    assert not (tmp_path / "firmware").exists()
    assert not list(tmp_path.glob(".firmware-staging-*"))


def test_differential_staging_validates_before_becoming_runnable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, payloads = _differential_request()
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )
    validated: list[tuple[MeasurementRequest, Path]] = []

    class Bench:
        def validate_differential_firmware(self, staged_request, runtime_dir):
            validated.append((staged_request, runtime_dir))

    daemon.bench = Bench()
    monkeypatch.setattr(daemon, "get_status", lambda: {})

    result = daemon.stage_firmware(request, payloads)

    assert result.state.value == "accepted"
    assert "bench validated" in result.message
    assert daemon.last_firmware_request is None
    assert validated == [(request, daemon.create_runtime_workspace(request.request_id))]
    assert (daemon.create_runtime_workspace(request.request_id) / "firmware" / "child").is_dir()

from datetime import datetime, timezone

import argparse
import hashlib
import json

import pytest

from cms_client import (
    CMSClient,
    CMSClientConfig,
    _print_measurement_result,
    _request_from_send_fw_args,
    build_cli_parser,
)
from cms_protocol import (
    MeasurementIdentity,
    MeasurementMode,
    MeasurementResult,
    MeasurementState,
)


def test_test_command_accepts_run_name():
    parser = build_cli_parser()

    named = parser.parse_args(["test", "default", "beacon", "--run-name", "Nightly Run"])

    assert named.run_name == "Nightly Run"


def test_run_test_sends_requested_run_name_to_daemon():
    captured = {}
    client = CMSClient.__new__(CMSClient)

    def fake_request(method, path, payload):
        captured.update(payload)
        return {"measurement_id": "generated-id", "state": "accepted"}

    client._request_json = fake_request

    result = client.run_test(
        MeasurementMode.DEFAULT,
        "beacon",
        run_name="Nightly Beacon Run",
    )

    assert captured["run_name"] == "Nightly Beacon Run"
    assert "request_id" not in captured
    assert result.measurement_id == "generated-id"


def test_measurement_display_includes_run_identity(capsys):
    identity = MeasurementIdentity(
        measurement_id="beacon-20260818T093000000000Z-abc123",
        run_name="Nightly Beacon Run",
        created_at=datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc),
    )

    _print_measurement_result(
        MeasurementResult(
            measurement_id=identity.measurement_id,
            state=MeasurementState.RUNNING,
            identity=identity,
        )
    )

    output = capsys.readouterr().out
    assert "run_name: Nightly Beacon Run" in output
    assert "created_at: 2026-08-18T09:30:00+00:00" in output


def test_firmware_request_uses_build_sdk_and_xapi_provenance(tmp_path, monkeypatch):
    build_root = tmp_path / "fw"
    runtime = build_root / "runtime"
    runtime.mkdir(parents=True)
    xapi = runtime / "sl_bt.xapi"
    xapi.write_bytes(b"exact-sdk-xapi")
    xapi_hash = hashlib.sha256(xapi.read_bytes()).hexdigest()
    (build_root / "fw_data.json").write_text(
        json.dumps({
            "firmware": {
                "board": "brd4182a",
                "device": "EFR32MG22C224F512IM40",
                "sisdk": "2026.1.0",
                "dut_app_hash": "dut-hash",
                "tester_app_hash": "tester-hash",
            },
            "build": {"xapi": str(xapi)},
            "sdk": {
                "source": "cli_package_path",
                "version": "2026.1.0",
                "commit": "sdk-commit",
                "hash": "sdk-hash",
                "xapi_sha256": xapi_hash,
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cms_client._discover_built_firmware",
        lambda role, firmware_cfg: tmp_path / f"{role}.s37",
    )
    args = argparse.Namespace(
        request_json="", request_id="request", dut_image="", tester_image="",
        bootloader_image="", dut_sha256="", tester_sha256="", bootloader_sha256="",
        board="", device="", git_commit="tool-commit", sisdk_version="",
        sisdk_commit="", requested_by="developer", build_root="",
    )

    request = _request_from_send_fw_args(
        args,
        CMSClientConfig(target={}, build={"root": str(build_root)}),
    )

    assert request.firmware.sisdk_source == "cli_package_path"
    assert request.firmware.sisdk_version == "2026.1.0"
    assert request.firmware.sisdk_commit == "sdk-commit"
    assert request.firmware.sisdk_hash == "sdk-hash"
    assert request.firmware.xapi_file_hash == xapi_hash
    assert request.firmware.dut_img.sha256 == "dut-hash"
    assert request.firmware.tester_img.sha256 == "tester-hash"

    args.sisdk_version = "2025.12.3"
    with pytest.raises(RuntimeError, match="does not match the firmware build provenance"):
        _request_from_send_fw_args(
            args,
            CMSClientConfig(target={}, build={"root": str(build_root)}),
        )


def test_firmware_request_rejects_incomplete_build_sdk_provenance(tmp_path):
    build_root = tmp_path / "fw"
    build_root.mkdir()
    (build_root / "fw_data.json").write_text(
        json.dumps({
            "firmware": {"sisdk": "2026.1.0"},
            "sdk": {"version": "2026.1.0"},
        }),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        request_json="", request_id="request", dut_image="", tester_image="",
        bootloader_image="", dut_sha256="", tester_sha256="", bootloader_sha256="",
        board="", device="", git_commit="", sisdk_version="", sisdk_commit="",
        requested_by="developer", build_root="",
    )

    with pytest.raises(RuntimeError, match="sdk.source, sdk.xapi_sha256"):
        _request_from_send_fw_args(
            args,
            CMSClientConfig(target={}, build={"root": str(build_root)}),
        )

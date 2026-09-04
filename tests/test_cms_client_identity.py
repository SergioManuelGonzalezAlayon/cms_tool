from datetime import datetime, timezone

import argparse
import hashlib
import json

import pytest

import cms_client
from cms_client import (
    CMSClient,
    CMSClientConfig,
    _failed_test_records_from_job_results,
    _print_measurement_result,
    _request_from_send_fw_args,
    _run_local_build,
    build_cli_parser,
)
from cms_colors import ColorFormatter
from cms_protocol import (
    MeasurementIdentity,
    MeasurementMode,
    MeasurementResult,
    MeasurementState,
    VariableFilter,
)


def test_test_command_accepts_run_name():
    parser = build_cli_parser()

    named = parser.parse_args(["test", "--mode", "default", "--feature", "beacon", "--run-name", "Nightly Run"])
    colored = parser.parse_args(["--color", "always", "test", "-m", "default", "-f", "beacon"])
    colored_after_command = parser.parse_args(["test", "--mode", "default", "--feature", "beacon", "--color", "never"])
    preview = parser.parse_args(["test", "--mode", "default", "--feature", "beacon", "--config-name", "dcdc", "--preview"])
    filtered = parser.parse_args(
        ["test", "--mode", "full_sweep", "--feature", "beacon", "--variable", "phy=2m", "--preview"]
    )
    all_features = parser.parse_args(["test", "-m", "default", "-f", "all"])

    assert named.run_name == "Nightly Run"
    assert colored.color == "always"
    assert colored_after_command.color == "never"
    assert preview.preview is True
    assert preview.config_name == "dcdc"
    assert filtered.variable == ["phy=2m"]
    assert all_features.feature == "all"


def test_tests_command_hides_debug_mode(capsys, monkeypatch):
    class Client:
        def list_tests(self):
            return {"modes": ["debug", "default", "full_sweep", "diff"], "features": []}

    monkeypatch.setattr("cms_client.CMSClient", lambda **kwargs: Client())

    exit_code = cms_client.main(["tests"])

    assert exit_code == 0
    assert capsys.readouterr().out == "modes: default, full_sweep, diff\n"


def test_build_derives_sisdk_version_from_the_selected_package(tmp_path, monkeypatch):
    sdk_path = tmp_path / "sdk"
    sdk_path.mkdir()
    (sdk_path / ".properties").write_text(
        "id=com.silabs.sdk.stack.sisdk\nversion=2025.12.3\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "cms_client_config.json"
    config_path.write_text(
        json.dumps({"target": {"sisdk_package_path": str(sdk_path)}}),
        encoding="utf-8",
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(
        cms_client.subprocess,
        "run",
        fake_run,
    )
    args = argparse.Namespace(
        action="build",
        targets=[],
        sdk_package_path="",
        build_root="",
        bootloader_slcp="",
    )

    assert _run_local_build(args, str(config_path)) == 0
    assert json.loads(config_path.read_text(encoding="utf-8"))["target"]["sisdk"] == "2025.12.3"
    assert "--sisdk-version" not in captured["command"]


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
    assert captured["config_hash"]
    assert "request_id" not in captured
    assert result.measurement_id == "generated-id"


def test_run_all_features_does_not_send_a_single_config_hash():
    captured = {}
    client = CMSClient.__new__(CMSClient)

    def fake_request(method, path, payload):
        captured.update(payload)
        return {"measurement_id": "generated-id", "state": "accepted"}

    client._request_json = fake_request

    client.run_test(MeasurementMode.DEFAULT, "all")

    assert captured["feature"] == "all"
    assert captured["config"] == "default"
    assert captured["config_hash"] == ""


def test_run_test_rejects_unknown_or_unsafe_local_config():
    client = CMSClient.__new__(CMSClient)
    client._request_json = lambda *args, **kwargs: {"measurement_id": "unused", "state": "accepted"}

    with pytest.raises(RuntimeError, match="test config does not exist"):
        client.run_test(MeasurementMode.DEFAULT, "beacon", config="missing")

    with pytest.raises(RuntimeError, match="single relative config path"):
        client.run_test(MeasurementMode.DEFAULT, "beacon", config="../default")

    with pytest.raises(RuntimeError, match="single feature name"):
        client.run_test(MeasurementMode.DEFAULT, "../beacon", config="default")


def test_test_preview_shows_exact_submission_without_contacting_daemon(capsys, monkeypatch):
    class Client:
        def run_test(self, *args, **kwargs):
            raise AssertionError("preview must not submit a test")

    monkeypatch.setattr("cms_client.CMSClient", lambda **kwargs: Client())

    exit_code = cms_client.main(
        ["test", "--mode", "default", "--feature", "beacon", "--config-name", "dcdc", "--preview"]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "mode: default" in output
    assert "feature: beacon" in output
    assert "config: dcdc" in output
    assert "config_path:" in output
    assert "tests\\configs\\ble\\beacon\\dcdc.json" in output or "tests/configs/ble/beacon/dcdc.json" in output
    assert "config_hash:" in output
    assert "expected_jobs: 1" in output
    assert "results_destination: database" in output


def test_test_command_rejects_removed_debug_mode():
    parser = build_cli_parser()

    with pytest.raises(SystemExit) as usage_error:
        parser.parse_args(["test", "--mode", "debug", "--feature", "beacon"])

    assert usage_error.value.code == 2


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["test", "--mode", "default", "--feature", "all", "--config-name", "dcdc"],
            "--feature all only supports the default config",
        ),
        (
            ["test", "--mode", "full_sweep", "--feature", "all", "--variable", "phy=2m"],
            "--feature all cannot be combined with --variable",
        ),
    ],
)
def test_all_feature_selection_rejects_ambiguous_scopes(argv, message):
    with pytest.raises(RuntimeError, match=message):
        cms_client._main(argv)


def test_test_preview_shows_variable_filters_from_parameters_json(capsys, monkeypatch):
    class Client:
        def run_test(self, *args, **kwargs):
            raise AssertionError("preview must not submit a test")

    monkeypatch.setattr("cms_client.CMSClient", lambda **kwargs: Client())

    exit_code = cms_client.main(
        ["test", "--mode", "full_sweep", "--feature", "beacon", "--variable", "phy=2m", "--preview"]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "variables: phy=2m" in output
    assert "expected_jobs:" in output
    assert "available_variables:" in output
    assert "phy: coded, 1m, 2m" in output


def test_test_command_passes_variable_filters_to_client(monkeypatch):
    captured = {}

    class Client:
        def run_test(self, *args, **kwargs):
            captured.update(kwargs)
            return MeasurementResult(
                measurement_id="",
                state=MeasurementState.REJECTED,
                message="daemon validation is a later step",
            )

    monkeypatch.setattr("cms_client.CMSClient", lambda **kwargs: Client())

    exit_code = cms_client.main(["test", "--mode", "full_sweep", "--feature", "beacon", "--variable", "phy=2m"])

    assert exit_code == int(cms_client.CMSCLIExitCode.TEST_REJECTED)
    assert captured["variable_filters"] == (VariableFilter("phy", "2m"),)


def test_run_test_sends_variable_filters_to_daemon():
    captured = {}
    client = CMSClient.__new__(CMSClient)

    def fake_request(method, path, payload):
        captured.update(payload)
        return {"measurement_id": "", "state": "rejected"}

    client._request_json = fake_request

    result = client.run_test(
        MeasurementMode.FULL_SWEEP,
        "beacon",
        variable_filters=(VariableFilter("phy", "2m"),),
    )

    assert captured["variable_filters"] == [{"name": "phy", "value": "2m"}]
    assert result.state is MeasurementState.REJECTED


def test_recipes_command_prints_copy_paste_workflows(capsys):
    exit_code = cms_client.main(
        [
            "RECIPES",
            "--feature",
            "beacon",
            "--config-name",
            "dcdc",
            "--measurement-id",
            "measurement-123",
            "--cursor",
            "17",
            "--out-dir",
            "downloads",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "cms TEST --mode default --feature beacon --config-name default --preview" in output
    assert "cms TEST --mode default --feature beacon --config-name dcdc --preview" in output
    assert "cms WATCH" in output
    assert "cms WATCH measurement-123 --cursor 17" in output
    assert "cms CANCEL" in output
    assert "cms FAILED measurement-123" in output
    assert "cms ARTIFACTS measurement-123 --out-dir downloads" in output


def test_exit_codes_command_prints_stable_cli_outcomes(capsys):
    exit_code = cms_client.main(["EXIT_CODES"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "0 SUCCESS" in output
    assert "1 TEST_FAILED" in output
    assert "completed test failure" in output
    assert "5 CONNECTION_FAILURE" in output
    assert "connection failure" in output
    assert "6 PROTOCOL_MISMATCH" in output
    assert "protocol mismatch" in output


def test_troubleshoot_command_prints_required_topics(capsys):
    exit_code = cms_client.main(["TROUBLESHOOT"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "health warnings" in output
    assert "missing hardware mappings" in output
    assert "stale or missing firmware" in output
    assert "unavailable named configs" in output
    assert "daemon connection failures" in output
    assert "truncated live output" in output
    assert "stalled hardware setup" in output
    assert "missing artifacts" in output


def test_troubleshoot_command_can_filter_one_topic(capsys):
    exit_code = cms_client.main(["TROUBLESHOOT", "--topic", "daemon"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "daemon connection failures" in output
    assert "health warnings" not in output


def test_failed_records_prefer_structured_completed_job_results():
    records = [
        {
            "record_type": "job_started",
            "job_id": "tests/ble.py::test_beacon|default.json",
            "status": "running",
            "feature": "beacon",
            "config": "default.json",
        },
        {
            "record_type": "job_completed",
            "job_id": "tests/ble.py::test_beacon|dcdc.json",
            "status": "failed",
            "feature": "beacon",
            "config": "dcdc.json",
            "mode": "one_factor",
            "partition": "",
            "config_hash": "hash-dcdc",
        },
        {
            "record_type": "job_completed",
            "job_id": "tests/ble.py::test_beacon|default.json",
            "status": "passed",
            "feature": "beacon",
            "config": "default.json",
        },
    ]

    failed = _failed_test_records_from_job_results(records)

    assert failed == [
        {
            "job_id": "tests/ble.py::test_beacon|dcdc.json",
            "feature": "beacon",
            "config": "dcdc.json",
            "mode": "one_factor",
            "partition": "",
            "config_hash": "hash-dcdc",
        }
    ]


def test_failed_command_prints_failed_identities_and_rerun_commands(capsys, monkeypatch):
    class Client:
        def failed_tests(self, measurement_id):
            assert measurement_id == "measurement-123"
            return [
                {
                    "job_id": "tests/ble.py::test_beacon|dcdc.json",
                    "feature": "beacon",
                    "config": "dcdc.json",
                    "mode": "one_factor",
                    "partition": "",
                    "config_hash": "hash-dcdc",
                }
            ]

    monkeypatch.setattr("cms_client.CMSClient", lambda **kwargs: Client())

    exit_code = cms_client.main(["FAILED", "measurement-123"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "failed tests for measurement-123" in output
    assert "beacon dcdc.json" in output
    assert "hash-dcdc" in output
    assert "cms TEST --mode default --feature beacon --config-name dcdc" in output


def test_failed_command_can_emit_machine_readable_json(capsys, monkeypatch):
    class Client:
        def failed_tests(self, measurement_id):
            return [
                {
                    "job_id": "job-1",
                    "feature": "beacon",
                    "config": "dcdc.json",
                    "mode": "one_factor",
                    "partition": "",
                    "config_hash": "hash-dcdc",
                }
            ]

    monkeypatch.setattr("cms_client.CMSClient", lambda **kwargs: Client())

    exit_code = cms_client.main(
        ["FAILED", "measurement-123", "--json", "--color", "always"]
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert exit_code == 0
    assert "\033[" not in output
    assert payload["measurement_id"] == "measurement-123"
    assert payload["failed_tests"][0]["config"] == "dcdc.json"
    assert payload["failed_tests"][0]["rerun_command"] == (
        "cms TEST --mode default --feature beacon --config-name dcdc"
    )


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


def test_measurement_display_can_force_or_disable_color(capsys):
    result = MeasurementResult(
        measurement_id="measurement-123",
        state=MeasurementState.PASSED,
    )

    _print_measurement_result(result, colors=ColorFormatter(True))
    forced_output = capsys.readouterr().out
    _print_measurement_result(result, colors=ColorFormatter(False))
    plain_output = capsys.readouterr().out

    assert "\033[" in forced_output
    assert "\033[" not in plain_output


def test_firmware_request_uses_build_sdk_and_xapi_provenance(tmp_path, monkeypatch):
    build_root = tmp_path / "fw"
    runtime = build_root / "runtime"
    runtime.mkdir(parents=True)
    dut_image = tmp_path / "dut.s37"
    no_dcdc_dut_image = tmp_path / "dut-no-dcdc.s37"
    tester_image = tmp_path / "tester.s37"
    dut_image.write_bytes(b"exact-dut-s37")
    no_dcdc_dut_image.write_bytes(b"exact-no-dcdc-dut-s37")
    tester_image.write_bytes(b"exact-tester-s37")
    dut_hash = hashlib.sha256(dut_image.read_bytes()).hexdigest()
    tester_hash = hashlib.sha256(tester_image.read_bytes()).hexdigest()
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
        lambda role, firmware_cfg: {
            "dut": dut_image,
            "dut_dcdc": dut_image,
            "dut_no_dcdc": no_dcdc_dut_image,
            "tester": tester_image,
        }[role],
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
    assert request.firmware.dut_img.sha256 == dut_hash
    assert request.firmware.tester_img.sha256 == tester_hash
    assert request.firmware.dut_variants["dcdc"].url == str(dut_image)
    assert request.firmware.dut_variants["no_dcdc"].url == str(no_dcdc_dut_image)

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

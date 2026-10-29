import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cms_bench import (
    BenchSession,
    _load_requested_test_config,
    _requested_test_config_path,
    _write_custom_config_job_plan,
    _write_custom_config_snapshot,
)
from cms_client import CMSClient
import cms_client
from cms_protocol import (
    FirmwareData,
    FirmwareImage,
    MeasurementMode,
    MeasurementRequest,
    SuiteSelector,
    measurement_request_from_dict,
    measurement_request_to_dict,
)
import test_orchestrator


def _local_custom_config() -> dict:
    return json.loads(
        Path("tests/configs/ble/beacon/custom.json").read_text(encoding="utf-8")
    )


def test_client_sends_validated_local_custom_config_without_a_hash(tmp_path, monkeypatch):
    config_path = tmp_path / "custom.json"
    config_data = _local_custom_config()
    config_path.write_text(json.dumps(config_data), encoding="utf-8")
    monkeypatch.setattr("cms_client._local_test_config_path", lambda _feature, _config: config_path)
    client = CMSClient(config_path=tmp_path / "missing-client-config.json")
    captured = {}

    def capture_request(method, path, payload):
        captured.update(method=method, path=path, payload=payload)
        return {"measurement_id": "measurement-1", "state": "accepted"}

    monkeypatch.setattr(client, "_request_json", capture_request)
    client.run_test(MeasurementMode.DEFAULT, "beacon", config="custom")

    assert captured["method"] == "POST"
    assert captured["path"] == "/test"
    assert captured["payload"]["custom_config_data"] == config_data
    assert captured["payload"]["config_hash"] == ""


def test_client_rejects_invalid_local_custom_config_before_submission(tmp_path, monkeypatch):
    config_path = tmp_path / "custom.json"
    config_path.write_text('{"advertiser":{"interval":1,"phy":"1m"}}', encoding="utf-8")
    monkeypatch.setattr("cms_client._local_test_config_path", lambda _feature, _config: config_path)
    client = CMSClient(config_path=tmp_path / "missing-client-config.json")
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda *_args, **_kwargs: pytest.fail("invalid custom config was submitted"),
    )

    with pytest.raises(RuntimeError, match="test config is invalid"):
        client.run_test(MeasurementMode.DEFAULT, "beacon", config="custom")


def test_custom_command_updates_config_then_runs_custom_test(tmp_path, monkeypatch):
    captured = {}
    fake_client = SimpleNamespace(base_url="http://bench")
    endpoint = SimpleNamespace(
        base_url="http://bench",
        api_token="token",
        timeout_s=5.0,
    )
    monkeypatch.setattr(cms_client, "resolve_client_endpoint", lambda *_args, **_kwargs: endpoint)
    monkeypatch.setattr(cms_client, "CMSClient", lambda **_kwargs: fake_client)
    monkeypatch.setattr(cms_client, "_print_explicit_endpoint_selection", lambda *_args: None)
    monkeypatch.setattr(
        cms_client,
        "update_custom_test_config",
        lambda feature, assignments: captured.update(
            feature=feature,
            assignments=assignments,
        ) or tmp_path / "custom.json",
    )

    def run_custom_test(client, args, variable_filters, _colors):
        captured.update(
            client=client,
            mode=args.mode,
            feature=args.feature,
            config_name=args.config_name,
            variable_filters=variable_filters,
        )
        return cms_client.CMSCLIExitCode.SUCCESS

    monkeypatch.setattr(cms_client, "_run_test_feature", run_custom_test)
    result = cms_client._main(
        [
            "CUSTOM",
            "--feature",
            "beacon",
            "--set",
            "advertiser.interval=100",
            "--mode",
            "diff",
        ]
    )

    assert result == cms_client.CMSCLIExitCode.SUCCESS
    assert captured["feature"] == "beacon"
    assert captured["assignments"] == ["advertiser.interval=100"]
    assert captured["client"] is fake_client
    assert captured["mode"] == "diff"
    assert captured["config_name"] == "custom"
    assert captured["variable_filters"] == ()


def test_custom_config_update_only_edits_local_custom_json(tmp_path, monkeypatch):
    config_dir = tmp_path / "tests" / "configs" / "ble" / "beacon"
    config_dir.mkdir(parents=True)
    config_data = _local_custom_config()
    custom_path = config_dir / "custom.json"
    custom_path.write_text(json.dumps(config_data, indent=2) + "\n", encoding="utf-8")
    other_config_paths = (config_dir / "default.json", config_dir / "extended.json")
    for path in other_config_paths:
        path.write_text(json.dumps(config_data, indent=2) + "\n", encoding="utf-8")
    other_config_contents = {path: path.read_bytes() for path in other_config_paths}
    monkeypatch.chdir(tmp_path)

    updated_path = cms_client.update_custom_test_config(
        "beacon",
        ["advertiser.interval=100"],
    )

    updated_config = json.loads(custom_path.read_text(encoding="utf-8"))
    assert updated_path == custom_path.resolve()
    assert updated_config["advertiser"]["interval"] == 100
    assert {
        path: path.read_bytes() for path in other_config_paths
    } == other_config_contents


def test_measurement_request_round_trip_preserves_custom_config_data():
    config_data = _local_custom_config()
    request = MeasurementRequest(
        request_id="measurement-1",
        selector=SuiteSelector(
            mode="ble",
            feature="beacon",
            config="custom",
            custom_config_data=config_data,
        ),
        firmware=FirmwareData(
            dut_img=FirmwareImage(name="dut.s37", url="dut.s37"),
            tester_img=FirmwareImage(name="tester.s37", url="tester.s37"),
        ),
    )

    restored = measurement_request_from_dict(measurement_request_to_dict(request))

    assert restored.selector.custom_config_data == config_data


def test_custom_job_plan_runs_the_transmitted_snapshot(tmp_path):
    config_data = _local_custom_config()
    request = MeasurementRequest(
        request_id="measurement-1",
        selector=SuiteSelector(
            mode="ble",
            feature="beacon",
            config="custom",
            custom_config_data=config_data,
        ),
        firmware=FirmwareData(
            dut_img=FirmwareImage(name="dut.s37", url="dut.s37"),
            tester_img=FirmwareImage(name="tester.s37", url="tester.s37"),
        ),
    )
    runtime_dir = tmp_path / "measurement"
    runtime_dir.mkdir()
    session = BenchSession(
        measurement_id=request.request_id,
        runtime_dir=runtime_dir,
        manifest=request.firmware,
        request=request,
    )
    snapshot_path = _write_custom_config_snapshot(
        runtime_dir / "custom_config_snapshot.json",
        config_data,
    )
    metadata_path = runtime_dir / "cms_fw_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "identity": {"measurement_id": request.request_id},
                "request": {"selector": {"config_hash": ""}},
                "firmware": {
                    "dut_slcp": config_data["firmware"]["dut_slcp"],
                    "tester_slcp": config_data["firmware"]["tester_slcp"],
                    "board": config_data["firmware"]["board"],
                },
            }
        ),
        encoding="utf-8",
    )

    plan_path = _write_custom_config_job_plan(
        session,
        metadata_path,
        runtime_dir / "orchestrator_results.jsonl",
    )
    session.job_plan_path = plan_path
    job = test_orchestrator.load_job_plan(plan_path)[0]

    assert job.config_path == snapshot_path
    assert job.cfg_arg == str(snapshot_path.resolve())
    assert job.config_hash
    assert _requested_test_config_path(session) == snapshot_path
    assert _load_requested_test_config(session) == config_data

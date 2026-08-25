import json
import hashlib
from datetime import datetime, timezone

import pytest

import cms_bench
from cms_bench import BenchResources, BenchSession, CMSBench
from cms_protocol import (
    FirmwareData,
    FirmwareImage,
    MeasurementIdentity,
    MeasurementRequest,
    MeasurementResult,
    MeasurementState,
    ResultType,
    SuiteSelector,
)


def _session(
    tmp_path,
    *,
    config: str = "default",
    config_hash: str = "",
) -> BenchSession:
    firmware = FirmwareData(
        dut_img=FirmwareImage(name="dut.s37", url="dut.s37", sha256="dut-hash"),
        tester_img=FirmwareImage(name="tester.s37", url="tester.s37", sha256="tester-hash"),
        xapi_file=FirmwareImage(name="sl_bt.xapi", url="sl_bt.xapi", sha256="xapi-hash"),
    )
    request = MeasurementRequest(
        request_id="measurement-123",
        selector=SuiteSelector(
            mode="ble",
            feature="beacon",
            config=config,
            config_hash=config_hash,
        ),
        firmware=firmware,
    )
    return BenchSession(
        measurement_id=request.request_id,
        runtime_dir=tmp_path,
        manifest=firmware,
        request=request,
    )


def test_pytest_runtime_metadata_contains_measurement_identity(tmp_path, monkeypatch):
    identity = MeasurementIdentity(
        measurement_id="beacon-20260818T093000000000Z-abc123",
        run_name="Nightly Beacon Run",
        created_at=datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc),
    )
    firmware = FirmwareData(
        dut_img=FirmwareImage(name="dut.s37", url="dut.s37", sha256="dut-hash"),
        tester_img=FirmwareImage(name="tester.s37", url="tester.s37", sha256="tester-hash"),
        xapi_file=FirmwareImage(name="sl_bt.xapi", url="sl_bt.xapi", sha256="xapi-hash"),
    )
    request = MeasurementRequest(
        request_id=identity.measurement_id,
        selector=SuiteSelector(mode="ble", feature="beacon", config="default"),
        firmware=firmware,
        identity=identity,
    )
    session = BenchSession(
        measurement_id=identity.measurement_id,
        runtime_dir=tmp_path,
        manifest=firmware,
        request=request,
    )
    for name in ("dut.s37", "tester.s37", "sl_bt.xapi"):
        (tmp_path / name).write_bytes(name.encode("ascii"))
    monkeypatch.setattr(
        cms_bench,
        "_load_requested_test_config",
        lambda session: {"firmware": {"dut_slcp": "dut.slcp", "tester_slcp": "tester.slcp"}},
    )

    prepared = cms_bench._prepare_pytest_runtime(session, BenchResources())
    metadata = json.loads(prepared["metadata_path"].read_text(encoding="utf-8"))

    assert metadata["identity"] == {
        "measurement_id": identity.measurement_id,
        "run_name": "Nightly Beacon Run",
        "created_at": "2026-08-18T09:30:00+00:00",
    }
    junit_dir = tmp_path / "junit"
    junit_dir.mkdir()
    junit_report = junit_dir / "0001-beacon-default.xml"
    junit_report.write_text("<testsuites />\n", encoding="utf-8")
    assert junit_report in cms_bench._run_diagnostic_artifacts(session)

    current_result = tmp_path / "beacon_result.json"
    current_result.write_text('{"result_type":"current_measurement"}\n', encoding="utf-8")
    monkeypatch.setattr(
        cms_bench,
        "_real_test_artifacts",
        lambda session, since=None: [current_result],
    )
    result = CMSBench().collect_artifacts(
        session,
        MeasurementResult(measurement_id=identity.measurement_id, state=MeasurementState.PASSED),
    )
    result_types = {artifact.path: artifact.result_type for artifact in result.artifacts}
    assert result_types[junit_report] is ResultType.PYTEST_JUNIT
    assert result_types[current_result] is ResultType.CURRENT_MEASUREMENT


def test_pytest_runtime_metadata_contains_requested_config_hash(tmp_path, monkeypatch):
    session = _session(tmp_path, config="dcdc", config_hash="sha256:dcdc")
    for name in ("dut.s37", "tester.s37", "sl_bt.xapi"):
        (tmp_path / name).write_bytes(name.encode("ascii"))
    monkeypatch.setattr(
        cms_bench,
        "_load_requested_test_config",
        lambda session: {"firmware": {"dut_slcp": "dut.slcp", "tester_slcp": "tester.slcp"}},
    )

    prepared = cms_bench._prepare_pytest_runtime(session, BenchResources())
    metadata = json.loads(prepared["metadata_path"].read_text(encoding="utf-8"))

    assert metadata["request"]["selector"] == {
        "mode": "ble",
        "feature": "beacon",
        "config": "dcdc",
        "config_hash": "sha256:dcdc",
    }


def test_pytest_runtime_records_selected_config_evidence_in_measurement_workspace(
    tmp_path,
    monkeypatch,
):
    config_root = tmp_path / "tests" / "configs" / "ble" / "beacon"
    config_root.mkdir(parents=True)
    config_path = config_root / "dcdc.json"
    config_text = '{"firmware":{"dut_slcp":"dut.slcp","tester_slcp":"tester.slcp"},"hw":{"supply_v":3.0}}\n'
    config_path.write_text(config_text, encoding="utf-8")
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    monkeypatch.chdir(tmp_path)
    session = _session(tmp_path / "runtime", config="dcdc", config_hash=config_hash)
    session.runtime_dir.mkdir()
    for name in ("dut.s37", "tester.s37", "sl_bt.xapi"):
        (session.runtime_dir / name).write_bytes(name.encode("ascii"))

    prepared = cms_bench._prepare_pytest_runtime(session, BenchResources())

    evidence_path = prepared["selected_config_metadata_path"]
    snapshot_path = prepared["selected_config_path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence_path.parent == session.runtime_dir
    assert snapshot_path.parent == session.runtime_dir
    assert evidence == {
        "mode": "ble",
        "feature": "beacon",
        "config": "dcdc",
        "selected_config": "dcdc.json",
        "requested_config_hash": config_hash,
        "observed_config_hash": config_hash,
        "source_path": str(config_path.resolve()),
        "snapshot_path": str(snapshot_path),
    }
    assert snapshot_path.read_text(encoding="utf-8") == config_text
    assert config_path.read_text(encoding="utf-8") == config_text
    assert snapshot_path not in cms_bench._run_diagnostic_artifacts(session)
    assert evidence_path not in cms_bench._run_diagnostic_artifacts(session)


def test_bench_rejects_config_path_traversal_before_execution(tmp_path):
    session = _session(tmp_path, config="../default")

    with pytest.raises(RuntimeError, match="single relative config path"):
        cms_bench._load_requested_test_config(session)


def test_bench_rejects_feature_path_traversal_before_execution(tmp_path):
    session = _session(tmp_path, config="default")
    session.request.selector.feature = "../beacon"

    with pytest.raises(RuntimeError, match="single feature name"):
        cms_bench._load_requested_test_config(session)


def test_bench_rejects_requested_config_hash_mismatch(tmp_path):
    session = _session(tmp_path, config="default", config_hash="not-the-default-hash")

    with pytest.raises(RuntimeError, match="test config hash mismatch"):
        cms_bench._load_requested_test_config(session)


def test_bench_rejects_invalid_config_schema(tmp_path, monkeypatch):
    config_root = tmp_path / "tests" / "configs" / "ble" / "beacon"
    config_root.mkdir(parents=True)
    (config_root / "default.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    session = _session(tmp_path, config="default")

    with pytest.raises(RuntimeError, match="test config must be a JSON object"):
        cms_bench._load_requested_test_config(session)


def test_bench_accepts_matching_requested_config_hash(tmp_path):
    config_path = cms_bench.Path("tests") / "configs" / "ble" / "beacon" / "default.json"
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    session = _session(tmp_path, config="default", config_hash=config_hash)

    assert isinstance(cms_bench._load_requested_test_config(session), dict)


def test_bench_runs_default_config_without_exact_config_override(tmp_path, monkeypatch):
    session = _session(tmp_path, config="default")
    captured = {}

    def fake_run_managed_process(command, **kwargs):
        captured["command"] = command
        return cms_bench.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(cms_bench, "_run_managed_process", fake_run_managed_process)

    cms_bench._run_test_orchestrator(
        session,
        tmp_path / "orchestrator_results.jsonl",
        tmp_path / "cms_fw_metadata.json",
    )

    assert "--exact-config" not in captured["command"]
    assert "--cfg" not in captured["command"]


def test_bench_runs_one_exact_named_config(tmp_path, monkeypatch):
    session = _session(tmp_path, config="dcdc")
    captured = {}

    def fake_run_managed_process(command, **kwargs):
        captured["command"] = command
        return cms_bench.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(cms_bench, "_run_managed_process", fake_run_managed_process)

    cms_bench._run_test_orchestrator(
        session,
        tmp_path / "orchestrator_results.jsonl",
        tmp_path / "cms_fw_metadata.json",
    )

    assert "--cfg" not in captured["command"]
    assert _option_value(captured["command"], "--exact-config") == "dcdc"


def _option_value(command: list[str], option: str) -> str:
    return command[command.index(option) + 1]

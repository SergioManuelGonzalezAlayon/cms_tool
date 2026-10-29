import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import cms_bench
from cms_bench import BenchResources, BenchSession, CMSBench
from cms_protocol import (
    BenchIdentity,
    FirmwareData,
    FirmwareImage,
    MeasurementIdentity,
    MeasurementMode,
    MeasurementRequest,
    MeasurementResult,
    MeasurementState,
    ResultType,
    SuiteSelector,
    VariableFilter,
)


_XAPI_BYTES = b"sl_bt.xapi"
_XAPI_SHA256 = hashlib.sha256(_XAPI_BYTES).hexdigest()


def _session(
    tmp_path,
    *,
    feature: str = "beacon",
    config: str = "default",
    config_hash: str = "",
    mode: str = "default",
    variable_filters: tuple[VariableFilter, ...] = (),
) -> BenchSession:
    firmware = FirmwareData(
        dut_img=FirmwareImage(name="dut.s37", url="dut.s37", sha256="dut-hash"),
        tester_img=FirmwareImage(name="tester.s37", url="tester.s37", sha256="tester-hash"),
        xapi_file=FirmwareImage(name="sl_bt.xapi", url="sl_bt.xapi", sha256=_XAPI_SHA256),
    )
    request = MeasurementRequest(
        request_id="measurement-123",
        selector=SuiteSelector(
            mode="ble",
            feature=feature,
            config=config,
            config_hash=config_hash,
            variable_filters=variable_filters,
        ),
        firmware=firmware,
        mode=MeasurementMode(mode),
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
        xapi_file=FirmwareImage(name="sl_bt.xapi", url="sl_bt.xapi", sha256=_XAPI_SHA256),
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
    retained_result = session.runtime_dir / "measurement_artifacts" / "beacon_result.json"
    assert result_types[retained_result] is ResultType.CURRENT_MEASUREMENT
    assert retained_result.read_text(encoding="utf-8") == '{"result_type":"current_measurement"}\n'


def test_differential_session_validates_firmware_from_its_active_root(tmp_path, monkeypatch):
    session = _session(tmp_path / "results" / "version1")
    active_root = tmp_path / "active"
    active_root.mkdir()
    session.active_firmware_dir = active_root
    validated_paths = []
    monkeypatch.setattr(
        cms_bench,
        "_validate_firmware_file",
        lambda path, _hash: validated_paths.append(path),
    )
    bench = CMSBench()
    bench.resources = BenchResources()

    bench.test_prepare(session)

    assert validated_paths == [
        active_root / "dut.s37",
        active_root / "tester.s37",
        active_root / "sl_bt.xapi",
    ]
    assert not list(session.runtime_dir.glob("*.s37"))


def test_setup_failure_retains_variant_status_summary_and_diagnostics(tmp_path, monkeypatch):
    session = _session(tmp_path / "results" / "version1")
    session.variant = "version1"
    bench = CMSBench()
    bench.initialized = True
    monkeypatch.setattr(bench, "test_smoke", lambda session: (_ for _ in ()).throw(RuntimeError("missing active DUT image")))

    result = bench.measurement_run(session)

    setup_status = json.loads((session.runtime_dir / "setup_status.json").read_text(encoding="utf-8"))
    run_summary = json.loads((session.runtime_dir / "cms_test_run.json").read_text(encoding="utf-8"))
    assert result.state is MeasurementState.FAILED
    assert result.message == "missing active DUT image"
    assert setup_status["variant"] == "version1"
    assert setup_status["phase"] == "validation"
    assert setup_status["pytest_launched"] is False
    assert setup_status["error"] == "missing active DUT image"
    assert run_summary["phase"] == "validation"
    assert run_summary["pytest_launched"] is False
    assert {artifact.path.name for artifact in result.artifacts} >= {
        "setup_status.json",
        "cms_test_run.json",
    }


def test_pytest_runtime_materializes_named_dut_variants(tmp_path, monkeypatch):
    session = _session(tmp_path)
    session.manifest.dut_variants = {
        "dcdc": FirmwareImage(name="dut-dcdc.s37", url="dut-dcdc.s37"),
        "no_dcdc": FirmwareImage(name="dut-no-dcdc.s37", url="dut-no-dcdc.s37"),
    }
    for name in ("dut.s37", "dut-dcdc.s37", "dut-no-dcdc.s37", "tester.s37", "sl_bt.xapi"):
        (tmp_path / name).write_bytes(name.encode("ascii"))
    monkeypatch.setattr(
        cms_bench,
        "_load_requested_test_config",
        lambda session: {
            "firmware": {
                "dut_slcp": "apps/ncp_ble_dut.slcp",
                "tester_slcp": "apps/ncp_ble_tester.slcp",
            }
        },
    )

    prepared = cms_bench._prepare_pytest_runtime(session, BenchResources())

    build_root = prepared["build_root"]
    assert (build_root / "ncp_ble_dut_dcdc" / "cmake_gcc" / "build" / "base" / "dut-dcdc.s37").read_bytes() == b"dut-dcdc.s37"
    assert (build_root / "ncp_ble_dut" / "cmake_gcc" / "build" / "base" / "dut-no-dcdc.s37").read_bytes() == b"dut-no-dcdc.s37"


def test_pytest_runtime_metadata_contains_requested_config_hash(tmp_path, monkeypatch):
    config_root = tmp_path / "tests" / "configs" / "ble" / "beacon"
    config_root.mkdir(parents=True)
    config_path = config_root / "dcdc.json"
    config_path.write_text(
        '{"firmware":{"dut_slcp":"dut.slcp","tester_slcp":"tester.slcp"}}\n',
        encoding="utf-8",
    )
    config_hash = cms_bench._sha256_text_file_lf_normalized(config_path)
    monkeypatch.chdir(tmp_path)
    session = _session(tmp_path, config="dcdc", config_hash=config_hash)
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
        "config_hash": config_hash,
        "variable_filters": [],
    }


def test_pytest_runtime_metadata_contains_staged_vcom_metadata(tmp_path, monkeypatch):
    session = _session(tmp_path)
    session.manifest.vcom = {
        "enabled": True,
        "gpio": {
            "port": "B",
            "pin": 4,
            "peripheral": "GPIO_NS",
            "dout_address": "0x5003C040",
            "pin_mask": "0x00000010",
        },
    }
    for name in ("dut.s37", "tester.s37", "sl_bt.xapi"):
        (tmp_path / name).write_bytes(name.encode("ascii"))
    monkeypatch.setattr(
        cms_bench,
        "_load_requested_test_config",
        lambda session: {"firmware": {"dut_slcp": "dut.slcp", "tester_slcp": "tester.slcp"}},
    )

    prepared = cms_bench._prepare_pytest_runtime(session, BenchResources())
    metadata = json.loads(prepared["metadata_path"].read_text(encoding="utf-8"))

    assert metadata["vcom"] == session.manifest.vcom


def test_pytest_runtime_metadata_contains_variable_filters(tmp_path, monkeypatch):
    session = _session(
        tmp_path,
        mode="full_sweep",
        variable_filters=(VariableFilter("phy", "2m"),),
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

    assert metadata["request"]["selector"]["variable_filters"] == [
        {"name": "phy", "value": "2m"}
    ]


def test_pytest_runtime_records_selected_config_evidence_in_measurement_workspace(
    tmp_path,
    monkeypatch,
):
    config_root = tmp_path / "tests" / "configs" / "ble" / "beacon"
    config_root.mkdir(parents=True)
    config_path = config_root / "dcdc.json"
    config_text = '{"firmware":{"dut_slcp":"dut.slcp","tester_slcp":"tester.slcp"},"hw":{"supply_v":3.0}}\n'
    config_path.write_text(config_text, encoding="utf-8")
    config_hash = cms_bench._sha256_text_file_lf_normalized(config_path)
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


def test_custom_config_evidence_records_the_observed_hash(tmp_path, monkeypatch):
    config_root = tmp_path / "tests" / "configs" / "ble" / "beacon"
    config_root.mkdir(parents=True)
    config_path = config_root / "custom.json"
    config_path.write_text('{"advertiser":{"interval":500}}\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    session = _session(tmp_path / "runtime", config="custom")
    custom_data = json.loads(
        (Path(__file__).resolve().parents[2] / "tests/configs/ble/beacon/custom.json")
        .read_text(encoding="utf-8")
    )
    session.request.selector.custom_config_data = custom_data
    submitted_snapshot = cms_bench._write_custom_config_snapshot(
        session.runtime_dir / "custom_config_snapshot.json",
        custom_data,
    )

    evidence_paths = cms_bench._write_selected_config_evidence(session)
    evidence = json.loads(evidence_paths["metadata_path"].read_text(encoding="utf-8"))

    assert evidence["selected_config"] == "custom.json"
    assert evidence["requested_config_hash"] == ""
    assert evidence["observed_config_hash"] == cms_bench._sha256_text_file_lf_normalized(submitted_snapshot)
    assert evidence_paths["snapshot_path"].read_text(encoding="utf-8") == submitted_snapshot.read_text(encoding="utf-8")


def test_config_hash_is_stable_across_line_endings(tmp_path):
    lf_config = tmp_path / "lf.json"
    crlf_config = tmp_path / "crlf.json"
    lf_config.write_bytes(b'{"firmware":{},"hw":{}}\n')
    crlf_config.write_bytes(b'{"firmware":{},"hw":{}}\r\n')

    assert cms_bench._sha256_text_file_lf_normalized(lf_config) == (
        cms_bench._sha256_text_file_lf_normalized(crlf_config)
    )
    assert cms_bench._sha256_file(lf_config) != cms_bench._sha256_file(crlf_config)


def test_bench_rejects_config_path_traversal_before_execution(tmp_path):
    session = _session(tmp_path, config="../default")

    with pytest.raises(RuntimeError, match="single relative config path"):
        cms_bench._load_requested_test_config(session)


def test_bench_rejects_feature_path_traversal_before_execution(tmp_path):
    session = _session(tmp_path, config="default")
    session.request.selector.feature = "../beacon"

    with pytest.raises(RuntimeError, match="single feature name"):
        cms_bench._load_requested_test_config(session)


def test_bench_accepts_requested_config_hash_mismatch(tmp_path):
    session = _session(tmp_path, config="default", config_hash="not-the-default-hash")

    assert isinstance(cms_bench._load_requested_test_config(session), dict)


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
    config_hash = cms_bench._sha256_text_file_lf_normalized(config_path)
    session = _session(tmp_path, config="default", config_hash=config_hash)

    assert isinstance(cms_bench._load_requested_test_config(session), dict)


def test_bench_accepts_firmware_device_with_package_suffix(tmp_path):
    session = _session(tmp_path)
    session.manifest.board = "brd4182a"
    session.manifest.device = "EFR32MG22C224F512IM40-C"
    _stage_manifest_files(session)

    bench = CMSBench(
        resources=BenchResources(
            board="brd4182a",
            device="EFR32MG22C224F512IM40",
        )
    )

    bench.test_prepare(session)


def test_bench_still_rejects_different_firmware_device(tmp_path):
    session = _session(tmp_path)
    session.manifest.board = "brd4182a"
    session.manifest.device = "EFR32MG24B310F1536IM48"
    _stage_manifest_files(session)

    bench = CMSBench(
        resources=BenchResources(
            board="brd4182a",
            device="EFR32MG22C224F512IM40",
        )
    )

    with pytest.raises(RuntimeError, match="does not match bench device"):
        bench.test_prepare(session)


def _stage_manifest_files(session: BenchSession) -> None:
    dut_path = session.runtime_dir / session.manifest.dut_img.name
    tester_path = session.runtime_dir / session.manifest.tester_img.name
    xapi_path = session.runtime_dir / session.manifest.xapi_file.name
    dut_path.write_bytes(b"dut")
    tester_path.write_bytes(b"tester")
    xapi_path.write_bytes(b"xapi")
    session.manifest.dut_img.sha256 = cms_bench._sha256_file(dut_path)
    session.manifest.tester_img.sha256 = cms_bench._sha256_file(tester_path)
    session.manifest.xapi_file.sha256 = cms_bench._sha256_file(xapi_path)


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


def test_bench_runs_all_features_from_the_ble_config_root(tmp_path, monkeypatch):
    session = _session(tmp_path, feature="all")
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

    assert cms_bench.Path(captured["command"][3]) == cms_bench.Path("tests") / "configs" / "ble"
    assert "--feature" not in captured["command"]


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


def test_bench_passes_variable_filters_to_orchestrator(tmp_path, monkeypatch):
    session = _session(
        tmp_path,
        mode="full_sweep",
        variable_filters=(VariableFilter("phy", "2m"), VariableFilter("dcdc", "On")),
    )
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

    assert _option_values(captured["command"], "--variable") == ["phy=2m", "dcdc=On"]


def test_bench_passes_frozen_plan_and_variant_to_generic_orchestrator(tmp_path, monkeypatch):
    session = _session(tmp_path, feature="all")
    session.variant = "version1"
    session.job_plan_path = tmp_path / "paired_default_plan.json"
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

    assert _option_value(captured["command"], "--job-plan") == str(session.job_plan_path)
    assert _option_value(captured["command"], "--variant") == "version1"


def test_generic_runner_passes_campaign_active_root_to_runtime_preparation(tmp_path, monkeypatch):
    session = _session(tmp_path)
    bench = CMSBench()
    bench.initialized = True
    bench.identity = BenchIdentity(bench_id="cms-bench-dummy-001")
    captured = {}
    monkeypatch.setattr(bench, "test_smoke", lambda session: None)
    monkeypatch.setattr(
        cms_bench,
        "_prepare_pytest_runtime",
        lambda session, resources, *, cms_bench_id="", active_root=None: captured.setdefault("prepared", {
            "results_log": tmp_path / "results.jsonl",
            "metadata_path": tmp_path / "metadata.json",
            "artifact_since": 0,
            "cms_bench_id": cms_bench_id,
            "active_root": active_root,
        }),
    )
    monkeypatch.setattr(
        cms_bench,
        "_run_test_orchestrator",
        lambda *args: cms_bench.subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(bench, "collect_artifacts", lambda session, result, **kwargs: result)

    active_root = tmp_path / "active"
    bench.measurement_run(session, active_root=active_root)

    assert captured["prepared"]["cms_bench_id"] == "cms-bench-dummy-001"
    assert captured["prepared"]["active_root"] == active_root


def test_bench_rejects_variable_filters_with_exact_config(tmp_path):
    session = _session(
        tmp_path,
        config="dcdc",
        mode="full_sweep",
        variable_filters=(VariableFilter("phy", "2m"),),
    )

    with pytest.raises(RuntimeError, match="cannot be combined with an exact config"):
        cms_bench._validate_variable_filtered_campaign(session)


def test_bench_rejects_variable_filters_that_select_zero_jobs(tmp_path, monkeypatch):
    config_root = _write_variable_filter_fixture(tmp_path, config_value="slow")
    monkeypatch.setattr("test_orchestrator.REPO_ROOT", tmp_path)
    monkeypatch.setattr("test_orchestrator.CONFIG_ROOT", tmp_path / "tests" / "configs")
    monkeypatch.chdir(tmp_path)
    session = _session(
        tmp_path,
        mode="full_sweep",
        variable_filters=(VariableFilter("rate", "fast"),),
    )

    with pytest.raises(RuntimeError, match="selected zero configs"):
        cms_bench._validate_variable_filtered_campaign(session)
    assert config_root.exists()


def test_bench_accepts_variable_filters_that_select_jobs(tmp_path, monkeypatch):
    _write_variable_filter_fixture(tmp_path, config_value="fast")
    monkeypatch.setattr("test_orchestrator.REPO_ROOT", tmp_path)
    monkeypatch.setattr("test_orchestrator.CONFIG_ROOT", tmp_path / "tests" / "configs")
    monkeypatch.chdir(tmp_path)
    session = _session(
        tmp_path,
        mode="full_sweep",
        variable_filters=(VariableFilter("rate", "fast"),),
    )

    cms_bench._validate_variable_filtered_campaign(session)


def _option_value(command: list[str], option: str) -> str:
    return command[command.index(option) + 1]


def _option_values(command: list[str], option: str) -> list[str]:
    values = []
    for index, item in enumerate(command):
        if item == option:
            values.append(command[index + 1])
    return values


def _write_variable_filter_fixture(tmp_path, *, config_value: str):
    config_root = tmp_path / "tests" / "configs" / "ble" / "beacon"
    sweep_root = config_root / "full_sweep" / "partition"
    sweep_root.mkdir(parents=True)
    (config_root / "parameters.json").write_text(
        json.dumps(
            {
                "generator": {"steps": 4},
                "parameters": [
                    {
                        "name": "rate",
                        "values": ["slow", "fast"],
                        "config_path": "radio.rate",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (sweep_root / "case.json").write_text(
        json.dumps({"radio": {"rate": config_value}}) + "\n",
        encoding="utf-8",
    )
    return config_root

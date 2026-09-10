import json
import hashlib
from pathlib import Path
from threading import Event

from cms_bench import CMSBench, DifferentialCampaignSession
from cms_protocol import (
    Artifacts,
    DifferentialFirmwareData,
    FirmwareData,
    FirmwareImage,
    FirmwareRoleImage,
    MeasurementRequest,
    MeasurementResult,
    MeasurementState,
    ResultType,
    SuiteSelector,
)
from cmsd import CMSDaemon, DaemonConfig, StagedDifferentialFirmware


def _request() -> MeasurementRequest:
    def variant(name: str) -> FirmwareData:
        dut = FirmwareImage(f"{name}-dut.s37", f"{name}-dut.s37", f"{name}-dut-hash")
        dut_no_dcdc = FirmwareImage(
            f"{name}-dut-no-dcdc.s37",
            f"{name}-dut-no-dcdc.s37",
            f"{name}-dut-no-dcdc-hash",
        )
        tester = FirmwareImage(f"{name}-tester.s37", f"{name}-tester.s37", f"{name}-tester-hash")
        return FirmwareData(
            dut_img=dut,
            tester_img=tester,
            dut_variants={"dcdc": dut, "no_dcdc": dut_no_dcdc},
            xapi_file=FirmwareImage(f"{name}.xapi", f"{name}.xapi", f"{name}-xapi-hash"),
            role_images=(
                FirmwareRoleImage("dut_dcdc", "ncp_ble_dut_dcdc", dut),
                FirmwareRoleImage("dut_no_dcdc", "ncp_ble_dut", dut_no_dcdc),
                FirmwareRoleImage("tester", "ncp_ble_tester", tester),
            ),
        )

    version1 = variant("version1")
    version2 = variant("version2")
    return MeasurementRequest(
        request_id="diff-contract-001",
        selector=SuiteSelector(mode="ble", feature="all"),
        firmware=version2,
        differential_firmware=DifferentialFirmwareData(version1=version1, version2=version2),
    )


def test_paired_plan_metadata_and_records_are_variant_isolated(tmp_path: Path) -> None:
    request = _request()
    runtime = tmp_path / "runtime"
    (runtime / "firmware").mkdir(parents=True)
    bench = CMSBench()

    plan_path = bench.build_differential_plan(request, runtime)
    version1_metadata = bench.prepare_differential_variant_metadata(request, runtime, "version1")
    version2_metadata = bench.prepare_differential_variant_metadata(request, runtime, "version2")
    version1_record = bench.record_differential_job(runtime, "version1", {"job_id": "job-1", "returncode": 0})
    version2_record = bench.record_differential_job(runtime, "version2", {"job_id": "job-1", "returncode": 1})
    campaign_path = bench.record_differential_campaign_order(runtime)

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["scope"] == "all_default_features"
    assert plan["jobs"]
    assert plan["record_type"] == "job_plan"
    assert plan["total_jobs"] == len(plan["jobs"])
    assert {"nodeid", "dut_slcp", "tester_slcp", "firmware_role"} <= set(plan["jobs"][0])
    assert version1_metadata.parent != version2_metadata.parent
    assert json.loads(version1_metadata.read_text(encoding="utf-8"))["variant"] == "version1"
    assert json.loads(version2_metadata.read_text(encoding="utf-8"))["variant"] == "version2"
    assert json.loads(version1_record.read_text(encoding="utf-8").splitlines()[0])["variant"] == "version1"
    assert json.loads(version2_record.read_text(encoding="utf-8").splitlines()[0])["returncode"] == 1
    assert json.loads(campaign_path.read_text(encoding="utf-8"))["run_order"] == ["version1", "version2"]


def test_differential_plan_honors_the_submitted_feature_selector(tmp_path: Path) -> None:
    request = _request()
    request.selector.feature = "beacon"
    runtime = tmp_path / "runtime"
    (runtime / "firmware").mkdir(parents=True)

    plan_path = CMSBench().build_differential_plan(request, runtime)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    assert plan["scope"] == "feature_default"
    assert plan["selector"] == {"feature": "beacon", "config": "default", "config_hash": ""}
    assert plan["jobs"]
    assert {job["feature"] for job in plan["jobs"]} == {"beacon"}


def test_differential_custom_config_is_frozen_for_both_firmware_passes(tmp_path: Path, monkeypatch) -> None:
    request = _request()
    request.selector.feature = "beacon"
    request.selector.config = "custom"
    config_dir = tmp_path / "tests" / "configs" / "ble" / "beacon"
    config_dir.mkdir(parents=True)
    (config_dir / "parameters.json").write_text("{}\n", encoding="utf-8")
    source_config = config_dir / "custom.json"
    original_content = '{"firmware":{"dut_slcp":"apps/ncp_ble_dut_dcdc.slcp","tester_slcp":"apps/ncp_ble_tester.slcp"},"hw":{"supply_v":3.0}}\n'
    source_config.write_text(original_content, encoding="utf-8")
    runtime = tmp_path / "runtime"
    (runtime / "firmware").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    import test_orchestrator

    monkeypatch.setattr(test_orchestrator, "CONFIG_ROOT", tmp_path / "tests" / "configs")

    plan_path = CMSBench().build_differential_plan(request, runtime)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    snapshot_path = runtime / "firmware" / "custom_config_snapshot.json"
    source_config.write_text('{"firmware":{"dut_slcp":"changed.slcp"}}\n', encoding="utf-8")
    jobs = test_orchestrator.load_job_plan(plan_path)

    assert snapshot_path.read_text(encoding="utf-8") == original_content
    assert plan["custom_config_snapshot"] == {
        "path": str(snapshot_path),
        "sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
    }
    assert jobs[0].config_path == snapshot_path
    assert jobs[0].cfg_arg == str(snapshot_path)
    assert jobs[0].config_hash == hashlib.sha256(snapshot_path.read_bytes()).hexdigest()


def test_daemon_lists_version1_and_version2_artifacts_without_cross_variant_loss(tmp_path: Path) -> None:
    request = _request()
    runtime = tmp_path / "runtime"
    (runtime / "results" / "version1").mkdir(parents=True)
    (runtime / "results" / "version2").mkdir(parents=True)
    (runtime / "results" / "version1" / "version1.json").write_text("{}", encoding="utf-8")
    (runtime / "results" / "version2" / "version2.json").write_text("{}", encoding="utf-8")
    daemon = CMSDaemon(
        DaemonConfig(runtime_root=tmp_path / "daemon", artifacts_root=tmp_path / "artifacts", bench_config=tmp_path / "bench.json")
    )

    assert [artifact.name for artifact in daemon.differential_result_artifacts(request, runtime)] == [
        "ble/all/diff/diff-contract-001/version1/version1.json",
        "ble/all/diff/diff-contract-001/version2/version2.json",
    ]


def test_daemon_retains_paired_plan_as_a_completed_measurement_artifact(tmp_path: Path) -> None:
    staged_runtime = tmp_path / "staged"
    execution_runtime = tmp_path / "execution"
    source = staged_runtime / "firmware" / "paired_default_plan.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"jobs": []}\n', encoding="utf-8")
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )

    request = _request()
    artifact = daemon.retain_differential_plan_artifact(request, staged_runtime, execution_runtime)

    assert artifact.name == "ble/all/diff/diff-contract-001/paired_default_plan.json"
    assert artifact.result_type is ResultType.CAMPAIGN
    assert artifact.path == execution_runtime / "evidence" / "ble" / "all" / "diff" / "diff-contract-001" / "paired_default_plan.json"
    assert artifact.path.read_text(encoding="utf-8") == '{"jobs": []}\n'


def test_daemon_retains_all_six_differential_role_images_by_measurement_id(tmp_path: Path) -> None:
    request = _request()
    staged_runtime = tmp_path / "staged"
    execution_runtime = tmp_path / "execution"
    for variant in ("version1", "version2"):
        for role_image in getattr(request.differential_firmware, variant).role_images:
            source = (
                staged_runtime / "firmware" / variant / "roles" / role_image.role
                / role_image.project_name / role_image.image.name
            )
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(role_image.image.sha256, encoding="utf-8")

    artifacts = CMSDaemon().retain_differential_firmware_evidence(
        request, staged_runtime, execution_runtime
    )

    assert len(artifacts) == 6
    assert {artifact.name.split("/")[5] for artifact in artifacts} == {
        "dut_dcdc",
        "dut_no_dcdc",
        "tester",
    }
    assert all(artifact.name.startswith("ble/all/diff/diff-contract-001/") for artifact in artifacts)
    assert all(artifact.path is not None and artifact.path.is_file() for artifact in artifacts)


def test_differential_terminal_artifacts_publish_before_the_consumed_stage_is_released(
    tmp_path: Path,
) -> None:
    request = _request()
    daemon = CMSDaemon(
        DaemonConfig(
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "artifacts",
            bench_config=tmp_path / "bench.json",
        )
    )
    staging_dir = tmp_path / "staging"
    manifest = staging_dir / "firmware" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    stage = StagedDifferentialFirmware(request, staging_dir, manifest)
    daemon.current_differential_firmware = stage

    execution_dir = tmp_path / "execution"
    plan_source = staging_dir / "firmware" / "paired_default_plan.json"
    plan_source.write_text('{"jobs": []}\n', encoding="utf-8")
    plan = daemon.retain_differential_plan_artifact(request, staging_dir, execution_dir)
    inputs = daemon.retain_differential_comparison_inputs(request, execution_dir)
    passes = daemon.retain_differential_pass_records(
        request,
        execution_dir,
        [
            {
                "variant": "version1",
                "state": "failed",
                "phase": "validation",
                "message": "missing active DUT image",
                "pytest_launched": False,
            }
        ],
    )
    campaign = execution_dir / "evidence" / "ble" / "all" / "diff" / "diff-contract-001" / "differential_campaign.json"
    campaign.parent.mkdir(parents=True, exist_ok=True)
    campaign.write_text('{"run_order": ["version1", "version2"]}\n', encoding="utf-8")
    result = MeasurementResult(
        measurement_id="completed-diff-001",
        state=MeasurementState.PASSED,
        artifacts=[
            plan,
            inputs,
            passes,
            Artifacts(
                name="ble/all/diff/diff-contract-001/differential_campaign.json",
                path=campaign,
                result_type=ResultType.CAMPAIGN,
            ),
        ],
    )

    published = daemon.publish_results(result)

    assert {artifact.name for artifact in published.artifacts} >= {
        "ble/all/diff/diff-contract-001/paired_default_plan.json",
        "ble/all/diff/diff-contract-001/differential_comparison_inputs.json",
        "ble/all/diff/diff-contract-001/differential_passes.json",
        "ble/all/diff/diff-contract-001/differential_campaign.json",
    }
    assert staging_dir.is_dir()
    daemon._release_consumed_differential_stage(stage)
    assert daemon.current_differential_firmware is None
    assert not staging_dir.exists()


def test_activate_differential_variant_materializes_a_normal_isolated_session(tmp_path: Path) -> None:
    request = _request()
    staged_runtime = tmp_path / "staged"
    execution_runtime = tmp_path / "execution"
    for variant_name, firmware in (
        ("version1", request.differential_firmware.version1),
        ("version2", request.differential_firmware.version2),
    ):
        no_dcdc = FirmwareImage(
            f"{variant_name}-no-dcdc.s37",
            f"{variant_name}-no-dcdc.s37",
            f"{variant_name}-no-dcdc-hash",
        )
        firmware.dut_variants["no_dcdc"] = no_dcdc
        firmware.role_images = (
            *(item for item in firmware.role_images if item.project_name != "ncp_ble_dut"),
            FirmwareRoleImage("dut_no_dcdc", "ncp_ble_dut", no_dcdc),
        )
        for role_image in firmware.role_images:
            source = (
                staged_runtime
                / "firmware"
                / variant_name
                / "roles"
                / role_image.role
                / role_image.project_name
                / role_image.image.name
            )
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(role_image.project_name, encoding="utf-8")
        xapi = staged_runtime / "firmware" / variant_name / "xapi" / firmware.xapi_file.name
        xapi.parent.mkdir(parents=True, exist_ok=True)
        xapi.write_text("xapi", encoding="utf-8")

    session = CMSBench().activate_differential_variant(
        request,
        DifferentialCampaignSession(
            measurement_id=request.request_id,
            staged_runtime_dir=staged_runtime,
            execution_runtime_dir=execution_runtime,
        ),
        "version1",
        cancel_event=Event(),
    )

    assert session.variant == "version1"
    assert session.runtime_dir == execution_runtime / "results" / "version1"
    assert session.firmware_root == execution_runtime / "active"
    assert set(session.manifest.dut_variants) == {"dcdc", "no_dcdc"}
    active_root = execution_runtime / "active"
    assert (active_root / "version1-dut.s37").is_file()
    assert (active_root / "version1-no-dcdc.s37").is_file()
    assert (active_root / "version1-tester.s37").is_file()
    assert not list(session.runtime_dir.glob("*.s37"))

    selections = CMSBench().report_differential_firmware_selection(session)

    assert len(selections) == 3
    assert all("version=version1" in line for line in selections)
    assert any("role=dut_dcdc project=ncp_ble_dut_dcdc" in line for line in selections)
    assert any("role=dut_no_dcdc project=ncp_ble_dut" in line for line in selections)
    assert any("role=tester project=ncp_ble_tester" in line for line in selections)


def test_differential_campaign_session_owns_active_and_isolated_result_roots(tmp_path: Path) -> None:
    campaign = DifferentialCampaignSession(
        measurement_id="diff-001",
        staged_runtime_dir=tmp_path / "staged",
        execution_runtime_dir=tmp_path / "execution",
    )

    assert campaign.run_order == ("version1", "version2")
    assert campaign.active_root == tmp_path / "execution" / "active"
    assert campaign.result_root("version1") == tmp_path / "execution" / "results" / "version1"
    assert campaign.result_root("version2") == tmp_path / "execution" / "results" / "version2"


def test_variant_activation_reuses_active_root_without_overwriting_variant_evidence(tmp_path: Path) -> None:
    request = _request()
    campaign = DifferentialCampaignSession(
        measurement_id=request.request_id,
        staged_runtime_dir=tmp_path / "staged",
        execution_runtime_dir=tmp_path / "execution",
    )
    for variant_name, firmware in (("version1", request.differential_firmware.version1), ("version2", request.differential_firmware.version2)):
        no_dcdc = FirmwareImage(f"{variant_name}-no.s37", "", "hash")
        firmware.role_images = (*firmware.role_images, FirmwareRoleImage("dut_no_dcdc", "ncp_ble_dut", no_dcdc))
        for item in firmware.role_images:
            path = campaign.staged_runtime_dir / "firmware" / variant_name / "roles" / item.role / item.project_name / item.image.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(variant_name, encoding="utf-8")
        xapi = campaign.staged_runtime_dir / "firmware" / variant_name / "xapi" / firmware.xapi_file.name
        xapi.parent.mkdir(parents=True, exist_ok=True)
        xapi.write_text(variant_name, encoding="utf-8")

    bench = CMSBench()
    version1 = bench.activate_differential_variant(request, campaign, "version1", cancel_event=Event())
    (version1.runtime_dir / "version1-evidence.json").write_text("version1", encoding="utf-8")
    version2 = bench.activate_differential_variant(request, campaign, "version2", cancel_event=Event())

    assert not (campaign.active_root / "version1-dut.s37").exists()
    assert (campaign.active_root / "version2-dut.s37").is_file()
    assert (version1.runtime_dir / "version1-evidence.json").read_text(encoding="utf-8") == "version1"
    assert version2.runtime_dir == campaign.result_root("version2")


def test_differential_status_is_published_to_the_main_measurement_stream(tmp_path: Path) -> None:
    daemon = CMSDaemon(DaemonConfig(runtime_root=tmp_path / "runtime", artifacts_root=tmp_path / "artifacts", bench_config=tmp_path / "bench.json"))
    measurement_id = "diff-status-001"
    daemon.create_runtime_workspace(measurement_id)
    daemon.measurements[measurement_id] = MeasurementResult(
        measurement_id=measurement_id,
        state=MeasurementState.RUNNING,
        progress_percent=50,
        current_step="running",
    )

    daemon._update_differential_status(
        measurement_id,
        current_variant="version1",
        completed_variants=0,
        current_test="beacon/default",
        passed_count=3,
        failed_count=1,
    )

    status = json.loads((daemon._runtime_workspace_path(measurement_id) / "current_status.json").read_text(encoding="utf-8"))
    events = (daemon._runtime_workspace_path(measurement_id) / "events.jsonl").read_text(encoding="utf-8")
    assert status["mode"] == "diff"
    assert status["current_variant"] == "version1"
    assert status["completed_variants"] == 0
    assert "differential_status" in events


def test_variant_prefixed_artifact_names_cannot_collide_when_published(tmp_path: Path) -> None:
    daemon = CMSDaemon(DaemonConfig(runtime_root=tmp_path / "runtime", artifacts_root=tmp_path / "artifacts", bench_config=tmp_path / "bench.json"))
    version1 = tmp_path / "runtime" / "results" / "version1" / "orchestrator_results.jsonl"
    version2 = tmp_path / "runtime" / "results" / "version2" / "orchestrator_results.jsonl"
    version1.parent.mkdir(parents=True)
    version2.parent.mkdir(parents=True)
    version1.write_text("version1\n", encoding="utf-8")
    version2.write_text("version2\n", encoding="utf-8")
    result = MeasurementResult(
        measurement_id="diff-artifacts-001",
        state=MeasurementState.PASSED,
        artifacts=[
            Artifacts(name="results/version1/orchestrator_results.jsonl", path=version1),
            Artifacts(name="results/version2/orchestrator_results.jsonl", path=version2),
        ],
    )

    published = daemon.publish_results(result)

    assert [artifact.name for artifact in published.artifacts[:2]] == [
        "results/version1/orchestrator_results.jsonl",
        "results/version2/orchestrator_results.jsonl",
    ]

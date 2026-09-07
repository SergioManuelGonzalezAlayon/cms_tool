import json
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
    SuiteSelector,
)
from cmsd import CMSDaemon, DaemonConfig


def _request() -> MeasurementRequest:
    def variant(name: str) -> FirmwareData:
        dut = FirmwareImage(f"{name}-dut.s37", f"{name}-dut.s37", f"{name}-dut-hash")
        tester = FirmwareImage(f"{name}-tester.s37", f"{name}-tester.s37", f"{name}-tester-hash")
        return FirmwareData(
            dut_img=dut,
            tester_img=tester,
            xapi_file=FirmwareImage(f"{name}.xapi", f"{name}.xapi", f"{name}-xapi-hash"),
            role_images=(
                FirmwareRoleImage("dut_dcdc", "ncp_ble_dut_dcdc", dut),
                FirmwareRoleImage("tester", "ncp_ble_tester", tester),
            ),
        )

    child = variant("child")
    parent = variant("parent")
    return MeasurementRequest(
        request_id="diff-contract-001",
        selector=SuiteSelector(mode="ble", feature="all"),
        firmware=child,
        differential_firmware=DifferentialFirmwareData(child=child, parent=parent),
    )


def test_paired_plan_metadata_and_records_are_variant_isolated(tmp_path: Path) -> None:
    request = _request()
    runtime = tmp_path / "runtime"
    (runtime / "firmware").mkdir(parents=True)
    bench = CMSBench()

    plan_path = bench.build_differential_plan(request, runtime)
    child_metadata = bench.prepare_differential_variant_metadata(request, runtime, "child")
    parent_metadata = bench.prepare_differential_variant_metadata(request, runtime, "parent")
    child_record = bench.record_differential_job(runtime, "child", {"job_id": "job-1", "returncode": 0})
    parent_record = bench.record_differential_job(runtime, "parent", {"job_id": "job-1", "returncode": 1})
    campaign_path = bench.record_differential_campaign_order(runtime)

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["scope"] == "all_default_features"
    assert plan["jobs"]
    assert plan["record_type"] == "job_plan"
    assert plan["total_jobs"] == len(plan["jobs"])
    assert {"nodeid", "dut_slcp", "tester_slcp", "firmware_role"} <= set(plan["jobs"][0])
    assert child_metadata.parent != parent_metadata.parent
    assert json.loads(child_metadata.read_text(encoding="utf-8"))["variant"] == "child"
    assert json.loads(parent_metadata.read_text(encoding="utf-8"))["variant"] == "parent"
    assert json.loads(child_record.read_text(encoding="utf-8").splitlines()[0])["variant"] == "child"
    assert json.loads(parent_record.read_text(encoding="utf-8").splitlines()[0])["returncode"] == 1
    assert json.loads(campaign_path.read_text(encoding="utf-8"))["run_order"] == ["parent", "child"]


def test_daemon_lists_parent_and_child_artifacts_without_cross_variant_loss(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "results" / "parent").mkdir(parents=True)
    (runtime / "results" / "child").mkdir(parents=True)
    (runtime / "results" / "parent" / "parent.json").write_text("{}", encoding="utf-8")
    (runtime / "results" / "child" / "child.json").write_text("{}", encoding="utf-8")
    daemon = CMSDaemon(
        DaemonConfig(runtime_root=tmp_path / "daemon", artifacts_root=tmp_path / "artifacts", bench_config=tmp_path / "bench.json")
    )

    assert [artifact.name for artifact in daemon.differential_result_artifacts(runtime)] == [
        "results/parent/parent.json",
        "results/child/child.json",
    ]


def test_activate_differential_variant_materializes_a_normal_isolated_session(tmp_path: Path) -> None:
    request = _request()
    staged_runtime = tmp_path / "staged"
    execution_runtime = tmp_path / "execution"
    for variant_name, firmware in (
        ("parent", request.differential_firmware.parent),
        ("child", request.differential_firmware.child),
    ):
        no_dcdc = FirmwareImage(
            f"{variant_name}-no-dcdc.s37",
            f"{variant_name}-no-dcdc.s37",
            f"{variant_name}-no-dcdc-hash",
        )
        firmware.role_images = (
            *firmware.role_images,
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
        "parent",
        cancel_event=Event(),
    )

    assert session.variant == "parent"
    assert session.runtime_dir == execution_runtime / "results" / "parent"
    assert set(session.manifest.dut_variants) == {"dcdc", "no_dcdc"}
    active_root = execution_runtime / "active"
    assert (active_root / "parent-dut.s37").is_file()
    assert (active_root / "parent-no-dcdc.s37").is_file()
    assert (active_root / "parent-tester.s37").is_file()


def test_differential_campaign_session_owns_active_and_isolated_result_roots(tmp_path: Path) -> None:
    campaign = DifferentialCampaignSession(
        measurement_id="diff-001",
        staged_runtime_dir=tmp_path / "staged",
        execution_runtime_dir=tmp_path / "execution",
    )

    assert campaign.run_order == ("parent", "child")
    assert campaign.active_root == tmp_path / "execution" / "active"
    assert campaign.result_root("parent") == tmp_path / "execution" / "results" / "parent"
    assert campaign.result_root("child") == tmp_path / "execution" / "results" / "child"


def test_variant_activation_reuses_active_root_without_overwriting_variant_evidence(tmp_path: Path) -> None:
    request = _request()
    campaign = DifferentialCampaignSession(
        measurement_id=request.request_id,
        staged_runtime_dir=tmp_path / "staged",
        execution_runtime_dir=tmp_path / "execution",
    )
    for variant_name, firmware in (("parent", request.differential_firmware.parent), ("child", request.differential_firmware.child)):
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
    parent = bench.activate_differential_variant(request, campaign, "parent", cancel_event=Event())
    (parent.runtime_dir / "parent-evidence.json").write_text("parent", encoding="utf-8")
    child = bench.activate_differential_variant(request, campaign, "child", cancel_event=Event())

    assert not (campaign.active_root / "parent-dut.s37").exists()
    assert (campaign.active_root / "child-dut.s37").is_file()
    assert (parent.runtime_dir / "parent-evidence.json").read_text(encoding="utf-8") == "parent"
    assert child.runtime_dir == campaign.result_root("child")


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
        current_variant="parent",
        completed_variants=0,
        current_test="beacon/default",
        passed_count=3,
        failed_count=1,
    )

    status = json.loads((daemon._runtime_workspace_path(measurement_id) / "current_status.json").read_text(encoding="utf-8"))
    events = (daemon._runtime_workspace_path(measurement_id) / "events.jsonl").read_text(encoding="utf-8")
    assert status["mode"] == "diff"
    assert status["current_variant"] == "parent"
    assert status["completed_variants"] == 0
    assert "differential_status" in events


def test_variant_prefixed_artifact_names_cannot_collide_when_published(tmp_path: Path) -> None:
    daemon = CMSDaemon(DaemonConfig(runtime_root=tmp_path / "runtime", artifacts_root=tmp_path / "artifacts", bench_config=tmp_path / "bench.json"))
    parent = tmp_path / "runtime" / "results" / "parent" / "orchestrator_results.jsonl"
    child = tmp_path / "runtime" / "results" / "child" / "orchestrator_results.jsonl"
    parent.parent.mkdir(parents=True)
    child.parent.mkdir(parents=True)
    parent.write_text("parent\n", encoding="utf-8")
    child.write_text("child\n", encoding="utf-8")
    result = MeasurementResult(
        measurement_id="diff-artifacts-001",
        state=MeasurementState.PASSED,
        artifacts=[
            Artifacts(name="results/parent/orchestrator_results.jsonl", path=parent),
            Artifacts(name="results/child/orchestrator_results.jsonl", path=child),
        ],
    )

    published = daemon.publish_results(result)

    assert [artifact.name for artifact in published.artifacts[:2]] == [
        "results/parent/orchestrator_results.jsonl",
        "results/child/orchestrator_results.jsonl",
    ]

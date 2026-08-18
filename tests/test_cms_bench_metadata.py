import json
from datetime import datetime, timezone

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

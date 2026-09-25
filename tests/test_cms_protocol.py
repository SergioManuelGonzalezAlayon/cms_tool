from datetime import datetime, timedelta, timezone

import pytest

from cms_protocol import (
    BenchHealthReport,
    BenchState,
    CMS_OBSERVATION_PROTOCOL_VERSION,
    CommanderProbeEvidence,
    DetectedTargetIdentity,
    DifferentialFirmwareData,
    EventCursor,
    FirmwareData,
    FirmwareImage,
    FirmwareRoleImage,
    MeasurementEvent,
    MeasurementEventPage,
    MeasurementIdentity,
    MeasurementMode,
    MeasurementRequest,
    MeasurementResult,
    MeasurementState,
    MeasurementSummary,
    SuiteSelector,
    TestCaseIdentity as CaseIdentity,
    TestCaseState as CaseState,
    TestCounts as Counts,
    VariableFilter,
    create_measurement_identity,
    bench_health_report_from_dict,
    bench_health_report_to_dict,
    event_cursor_from_int,
    event_cursor_to_int,
    measurement_event_from_dict,
    measurement_event_page_from_dict,
    measurement_event_page_to_dict,
    measurement_event_to_dict,
    measurement_identity_from_dict,
    measurement_identity_to_dict,
    measurement_request_from_dict,
    measurement_request_to_dict,
    measurement_result_from_dict,
    measurement_result_to_dict,
    measurement_summary_from_dict,
    measurement_summary_to_dict,
    test_case_identity_from_dict as case_identity_from_dict,
    test_case_identity_to_dict as case_identity_to_dict,
    test_counts_from_dict as counts_from_dict,
    test_counts_to_dict as counts_to_dict,
)


def test_test_case_states_are_stable_protocol_values():
    assert {state.value for state in CaseState} == {
        "pending",
        "running",
        "passed",
        "failed",
        "skipped",
        "cancelled",
    }


def test_measurement_identity_round_trip_preserves_utc_timestamp():
    identity = MeasurementIdentity(
        measurement_id="measurement-123",
        run_name="beacon-default",
        created_at=datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc),
    )

    payload = measurement_identity_to_dict(identity)

    assert payload == {
        "measurement_id": "measurement-123",
        "run_name": "beacon-default",
        "created_at": "2026-08-18T09:30:00+00:00",
    }
    assert measurement_identity_from_dict(payload) == identity


def test_create_measurement_identity_has_readable_name_utc_timestamp_and_suffix():
    identity = create_measurement_identity(
        "Beacon / Default Run",
        created_at=datetime(2026, 8, 18, 12, 30, tzinfo=timezone(timedelta(hours=3))),
        suffix="A1B2C3",
    )

    assert identity.run_name == "Beacon / Default Run"
    assert identity.created_at == datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc)
    assert identity.measurement_id == "beacon-default-run-20260818T093000000000Z-a1b2c3"


def test_create_measurement_identity_uses_collision_safe_suffixes():
    created_at = datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc)

    first = create_measurement_identity("beacon", created_at=created_at)
    second = create_measurement_identity("beacon", created_at=created_at)

    assert first.measurement_id != second.measurement_id


def test_create_measurement_identity_rejects_naive_timestamp():
    with pytest.raises(ValueError, match="timezone"):
        create_measurement_identity("beacon", created_at=datetime(2026, 8, 18, 9, 30))


def test_health_report_round_trips_optional_detected_identity_and_probe_evidence():
    report = BenchHealthReport(
        state=BenchState.IDLE,
        detected_identity=DetectedTargetIdentity(
            radio_board_part_number="BRD4182A",
            radio_board_revision="B06",
            radio_board_serial_number="212300218",
            target_device="EFR32MG22C224F512IM40-C",
            canonical_target_device="EFR32MG22C224F512IM40",
            dut_jlink_serial="440365029",
            vcom_supported=True,
        ),
        commander_probe=CommanderProbeEvidence(
            command=["commander", "adapter", "probe", "-s", "440365029"],
            returncode=0,
            stdout="Board List:\\n...",
        ),
    )

    payload = bench_health_report_to_dict(report)
    recovered = bench_health_report_from_dict(payload)

    assert payload["detected_identity"]["canonical_target_device"] == "EFR32MG22C224F512IM40"
    assert payload["commander_probe"]["command"][-1] == "440365029"
    assert recovered.detected_identity == report.detected_identity
    assert recovered.commander_probe == report.commander_probe


def test_health_report_accepts_legacy_payload_without_probe_fields():
    report = bench_health_report_from_dict({"state": "idle", "identity": {"bench_id": "bench-1"}})

    assert report.detected_identity is None
    assert report.commander_probe is None
    payload = bench_health_report_to_dict(report)
    assert "detected_identity" not in payload
    assert "commander_probe" not in payload


def test_measurement_result_round_trip_preserves_identity():
    identity = create_measurement_identity(
        "beacon-default",
        created_at=datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc),
        suffix="abc123",
    )
    result = MeasurementResult(
        measurement_id=identity.measurement_id,
        state=MeasurementState.RUNNING,
        started_at=identity.created_at,
        identity=identity,
    )

    assert measurement_result_from_dict(measurement_result_to_dict(result)) == result


def test_interrupted_measurement_state_is_serialized_as_terminal_outcome():
    result = MeasurementResult(
        measurement_id="measurement-123",
        state=MeasurementState.INTERRUPTED,
        progress_percent=100,
        current_step="interrupted",
        message="daemon restarted before completion",
    )

    payload = measurement_result_to_dict(result)

    assert payload["state"] == "interrupted"
    assert measurement_result_from_dict(payload).state is MeasurementState.INTERRUPTED


def test_test_case_identity_round_trip_preserves_plan_order_and_config_identity():
    identity = CaseIdentity(
        job_id="tests/ble.py::test_beacon|beacon/default.json",
        ordinal=2,
        total_jobs=5,
        feature="beacon",
        config="beacon/default.json",
        config_hash="abc123",
        mode="default",
        partition="default",
    )

    assert case_identity_from_dict(case_identity_to_dict(identity)) == identity


def test_measurement_request_round_trip_preserves_requested_config_hash():
    request = MeasurementRequest(
        request_id="measurement-123",
        selector=SuiteSelector(
            mode="ble",
            feature="beacon",
            config="dcdc",
            config_hash="sha256:dcdc",
        ),
        firmware=FirmwareData(
            dut_img=FirmwareImage(name="dut.s37", url="dut.s37"),
            tester_img=FirmwareImage(name="tester.s37", url="tester.s37"),
            dut_variants={
                "dcdc": FirmwareImage(name="dut-dcdc.s37", url="dut-dcdc.s37"),
                "no_dcdc": FirmwareImage(name="dut-no-dcdc.s37", url="dut-no-dcdc.s37"),
            },
        ),
        mode=MeasurementMode.DEFAULT,
    )

    payload = measurement_request_to_dict(request)

    assert payload["selector"]["config_hash"] == "sha256:dcdc"
    assert measurement_request_from_dict(payload).selector.config_hash == "sha256:dcdc"
    assert measurement_request_from_dict(payload).firmware.dut_variants == request.firmware.dut_variants


def _differential_variant(name: str, *, board: str = "brd4182a") -> FirmwareData:
    dut = FirmwareImage(name=f"{name}-dut.s37", url=f"{name}-dut.s37", sha256=f"{name}-dut-hash")
    tester = FirmwareImage(name=f"{name}-tester.s37", url=f"{name}-tester.s37", sha256=f"{name}-tester-hash")
    xapi = FirmwareImage(name=f"{name}-sl_bt.xapi", url=f"{name}-sl_bt.xapi", sha256=f"{name}-xapi-hash")
    return FirmwareData(
        dut_img=dut,
        tester_img=tester,
        xapi_file=xapi,
        board=board,
        device="EFR32MG22C224F512IM40",
        sisdk_version=f"{name}-sdk",
        sisdk_hash=f"{name}-sdk-hash",
        role_images=(
            FirmwareRoleImage("dut_dcdc", "ncp_ble_dut_dcdc", dut),
            FirmwareRoleImage("tester", "ncp_ble_tester", tester),
        ),
    )


def test_measurement_request_round_trip_preserves_named_differential_firmware() -> None:
    differential = DifferentialFirmwareData(
        version1=_differential_variant("version1"),
        version2=_differential_variant("version2"),
    )
    request = MeasurementRequest(
        request_id="diff-001",
        selector=SuiteSelector(mode="ble", feature="beacon"),
        firmware=differential.version2,
        differential_firmware=differential,
    )

    payload = measurement_request_to_dict(request)
    parsed = measurement_request_from_dict(payload)

    assert set(payload["differential_firmware"]) == {"version1", "version2"}
    assert parsed.differential_firmware is not None
    assert parsed.differential_firmware.version2.sisdk_version == "version2-sdk"
    assert parsed.differential_firmware.version1.role_images[0].role == "dut_dcdc"


def test_differential_firmware_rejects_missing_hashes_and_mismatched_targets() -> None:
    version2 = _differential_variant("version2")
    version2.dut_img.sha256 = ""
    with pytest.raises(ValueError, match="image without sha256"):
        DifferentialFirmwareData(version1=_differential_variant("version1"), version2=version2)

    with pytest.raises(ValueError, match="targets do not match"):
        DifferentialFirmwareData(
            version1=_differential_variant("version1", board="brd4182a"),
            version2=_differential_variant("version2", board="brd4182b"),
        )


def test_measurement_request_round_trip_preserves_variable_filters():
    request = MeasurementRequest(
        request_id="measurement-123",
        selector=SuiteSelector(
            mode="ble",
            feature="beacon",
            config="default",
            variable_filters=(
                VariableFilter(name="phy", value="2m"),
                VariableFilter(name="dcdc", value="On"),
            ),
        ),
        firmware=FirmwareData(
            dut_img=FirmwareImage(name="dut.s37", url="dut.s37"),
            tester_img=FirmwareImage(name="tester.s37", url="tester.s37"),
        ),
        mode=MeasurementMode.FULL_SWEEP,
    )

    payload = measurement_request_to_dict(request)

    assert payload["selector"]["variable_filters"] == [
        {"name": "phy", "value": "2m"},
        {"name": "dcdc", "value": "On"},
    ]
    assert measurement_request_from_dict(payload).selector.variable_filters == (
        VariableFilter(name="phy", value="2m"),
        VariableFilter(name="dcdc", value="On"),
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [("", "2m"), ("phy", ""), ("phy=value", "2m")],
)
def test_variable_filter_rejects_invalid_contract_values(name, value):
    with pytest.raises(ValueError):
        VariableFilter(name=name, value=value)


@pytest.mark.parametrize(
    ("ordinal", "total_jobs"),
    [(0, 1), (1, 0), (2, 1)],
)
def test_test_case_identity_rejects_invalid_plan_positions(ordinal, total_jobs):
    with pytest.raises(ValueError):
        CaseIdentity(
            job_id="job",
            ordinal=ordinal,
            total_jobs=total_jobs,
            feature="beacon",
            config="default.json",
        )


def test_test_counts_include_derived_remaining_count_and_round_trip():
    counts = Counts(total=8, completed=5, passed=2, failed=1, skipped=1)

    payload = counts_to_dict(counts)

    assert payload == {
        "total": 8,
        "completed": 5,
        "passed": 2,
        "failed": 1,
        "skipped": 1,
        "remaining": 3,
    }
    assert counts_from_dict(payload) == counts


@pytest.mark.parametrize(
    "counts",
    [
        {"total": -1},
        {"total": 1, "completed": 2},
        {"total": 2, "completed": 1, "passed": 1, "failed": 1},
        {"total": 2, "completed": 1, "remaining": 0},
    ],
)
def test_test_counts_reject_inconsistent_values(counts):
    with pytest.raises(ValueError):
        counts_from_dict(counts)


def test_event_cursor_is_non_negative_ordered_and_advances_by_one():
    cursor = EventCursor()

    next_cursor = cursor.next()

    assert cursor.value == 0
    assert next_cursor == EventCursor(1)
    assert next_cursor > cursor
    assert event_cursor_from_int(event_cursor_to_int(next_cursor)) == next_cursor


def test_event_cursor_rejects_negative_values():
    with pytest.raises(ValueError, match="cannot be negative"):
        EventCursor(-1)


def test_measurement_summary_round_trip_preserves_structured_observation_state():
    identity = MeasurementIdentity(
        measurement_id="measurement-123",
        run_name="Beacon Run",
        created_at=datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc),
    )
    current_test = CaseIdentity(
        job_id="job-3",
        ordinal=3,
        total_jobs=5,
        feature="beacon",
        config="beacon/third.json",
    )
    failed_test = CaseIdentity(
        job_id="job-2",
        ordinal=2,
        total_jobs=5,
        feature="beacon",
        config="beacon/second.json",
    )
    summary = MeasurementSummary(
        measurement_id=identity.measurement_id,
        identity=identity,
        state=MeasurementState.RUNNING,
        counts=Counts(total=5, completed=2, passed=1, failed=1),
        current_test=current_test,
        failed_tests=(failed_test,),
    )

    payload = measurement_summary_to_dict(summary)

    assert payload["protocol_version"] == CMS_OBSERVATION_PROTOCOL_VERSION
    assert payload["counts"]["remaining"] == 3
    assert payload["current_test"]["job_id"] == "job-3"
    assert payload["failed_tests"][0]["job_id"] == "job-2"
    assert measurement_summary_from_dict(payload) == summary


@pytest.mark.parametrize(
    "summary",
    [
        MeasurementSummary(
            measurement_id="measurement-123",
            state=MeasurementState.RUNNING,
            counts=Counts(total=1),
        ),
    ],
)
def test_measurement_summary_requires_matching_identity_and_failed_counts(summary):
    mismatched_identity = MeasurementIdentity(
        measurement_id="different-measurement",
        run_name="Different",
        created_at=datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc),
    )
    failed_test = CaseIdentity(
        job_id="job-1",
        ordinal=1,
        total_jobs=1,
        feature="beacon",
        config="default.json",
    )

    with pytest.raises(ValueError, match="identity must match"):
        MeasurementSummary(
            measurement_id=summary.measurement_id,
            identity=mismatched_identity,
            state=summary.state,
            counts=summary.counts,
        )
    with pytest.raises(ValueError, match="failed test identities"):
        MeasurementSummary(
            measurement_id=summary.measurement_id,
            state=summary.state,
            counts=Counts(total=1, completed=1, failed=1),
            failed_tests=(),
        )
    with pytest.raises(ValueError, match="failed test identities"):
        MeasurementSummary(
            measurement_id=summary.measurement_id,
            state=summary.state,
            counts=summary.counts,
            failed_tests=(failed_test,),
        )


def test_measurement_event_page_round_trip_preserves_cursor_and_event_details():
    events = (
        MeasurementEvent(
            measurement_id="measurement-123",
            cursor=EventCursor(4),
            timestamp=datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc),
            event_type="lifecycle",
            details={"state": "process_started", "pid": 42},
        ),
        MeasurementEvent(
            measurement_id="measurement-123",
            cursor=EventCursor(5),
            timestamp=datetime(2026, 8, 21, 9, 31, tzinfo=timezone.utc),
            event_type="console",
            details={"source": "stderr", "text": "warning\n"},
        ),
    )
    page = MeasurementEventPage(
        measurement_id="measurement-123",
        events=events,
        next_cursor=EventCursor(5),
        oldest_retained_cursor=EventCursor(4),
        truncated=True,
    )

    payload = measurement_event_page_to_dict(page)

    assert payload["protocol_version"] == CMS_OBSERVATION_PROTOCOL_VERSION
    assert payload["events"][0]["state"] == "process_started"
    assert payload["events"][1]["source"] == "stderr"
    assert payload["next_cursor"] == 5
    assert payload["oldest_retained_cursor"] == 4
    assert payload["truncated"] is True
    assert measurement_event_page_from_dict(payload) == page
    assert measurement_event_from_dict(measurement_event_to_dict(events[0])) == events[0]


def test_measurement_event_page_rejects_inconsistent_ids_order_and_next_cursor():
    first = MeasurementEvent(
        measurement_id="measurement-123",
        cursor=EventCursor(2),
        timestamp=datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc),
        event_type="lifecycle",
    )
    earlier = MeasurementEvent(
        measurement_id="measurement-123",
        cursor=EventCursor(1),
        timestamp=datetime(2026, 8, 21, 9, 29, tzinfo=timezone.utc),
        event_type="lifecycle",
    )
    other_measurement = MeasurementEvent(
        measurement_id="measurement-456",
        cursor=EventCursor(2),
        timestamp=datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc),
        event_type="lifecycle",
    )

    with pytest.raises(ValueError, match="ids must match"):
        MeasurementEventPage(
            measurement_id="measurement-123",
            events=(other_measurement,),
            next_cursor=EventCursor(2),
            oldest_retained_cursor=EventCursor(1),
        )
    with pytest.raises(ValueError, match="strictly increasing"):
        MeasurementEventPage(
            measurement_id="measurement-123",
            events=(first, earlier),
            next_cursor=EventCursor(1),
            oldest_retained_cursor=EventCursor(1),
        )
    with pytest.raises(ValueError, match="last returned event"):
        MeasurementEventPage(
            measurement_id="measurement-123",
            events=(first,),
            next_cursor=EventCursor(3),
            oldest_retained_cursor=EventCursor(1),
        )


def test_observation_contracts_reject_invalid_version_and_naive_event_timestamp():
    with pytest.raises(ValueError, match="protocol version"):
        MeasurementEventPage(
            measurement_id="measurement-123",
            events=(),
            next_cursor=EventCursor(),
            oldest_retained_cursor=EventCursor(),
            protocol_version=0,
        )
    with pytest.raises(ValueError, match="timezone"):
        MeasurementEvent(
            measurement_id="measurement-123",
            cursor=EventCursor(1),
            timestamp=datetime(2026, 8, 21, 9, 30),
            event_type="console",
        )

from datetime import datetime, timedelta, timezone

import pytest

from cms_protocol import (
    CMS_OBSERVATION_PROTOCOL_VERSION,
    EventCursor,
    FirmwareData,
    FirmwareImage,
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
    create_measurement_identity,
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
        ),
        mode=MeasurementMode.DEFAULT,
    )

    payload = measurement_request_to_dict(request)

    assert payload["selector"]["config_hash"] == "sha256:dcdc"
    assert measurement_request_from_dict(payload).selector.config_hash == "sha256:dcdc"


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

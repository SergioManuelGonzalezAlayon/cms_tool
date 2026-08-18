from datetime import datetime, timedelta, timezone

import pytest

from cms_protocol import (
    EventCursor,
    MeasurementIdentity,
    MeasurementResult,
    MeasurementState,
    TestCaseIdentity as CaseIdentity,
    TestCaseState as CaseState,
    TestCounts as Counts,
    create_measurement_identity,
    event_cursor_from_int,
    event_cursor_to_int,
    measurement_identity_from_dict,
    measurement_identity_to_dict,
    measurement_result_from_dict,
    measurement_result_to_dict,
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

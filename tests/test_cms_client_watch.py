from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cms_client import (
    MeasurementWatchState,
    poll_measurement_watch,
    watch_measurement,
)
from cms_protocol import (
    EventCursor,
    MeasurementEvent,
    MeasurementEventPage,
    MeasurementState,
    MeasurementSummary,
    TestCounts as Counts,
)


MEASUREMENT_ID = "measurement-123"


def _summary(state: MeasurementState, *, completed: int = 0) -> MeasurementSummary:
    return MeasurementSummary(
        measurement_id=MEASUREMENT_ID,
        state=state,
        counts=Counts(
            total=2,
            completed=completed,
            passed=completed,
        ),
    )


def _event(cursor: int, text: str) -> MeasurementEvent:
    return MeasurementEvent(
        measurement_id=MEASUREMENT_ID,
        cursor=EventCursor(cursor),
        timestamp=datetime(2026, 8, 21, 9, 30, cursor, tzinfo=timezone.utc),
        event_type="console",
        details={"source": "stdout", "text": text},
    )


def _page(
    *events: MeasurementEvent,
    next_cursor: int | None = None,
    oldest_retained_cursor: int = 1,
    truncated: bool = False,
) -> MeasurementEventPage:
    resolved_next_cursor = (
        next_cursor
        if next_cursor is not None
        else (events[-1].cursor.value if events else 0)
    )
    return MeasurementEventPage(
        measurement_id=MEASUREMENT_ID,
        events=events,
        next_cursor=EventCursor(resolved_next_cursor),
        oldest_retained_cursor=EventCursor(oldest_retained_cursor),
        truncated=truncated,
    )


class FakeWatchClient:
    def __init__(self, summaries, pages):
        self.summaries = list(summaries)
        self.pages = list(pages)
        self.requested_cursors = []
        self.cancel_calls = 0

    def get_measurement_summary(self, measurement_id):
        assert measurement_id == MEASUREMENT_ID
        next_summary = self.summaries.pop(0)
        if isinstance(next_summary, Exception):
            raise next_summary
        return next_summary

    def get_measurement_events(self, measurement_id, after_cursor):
        assert measurement_id == MEASUREMENT_ID
        self.requested_cursors.append(after_cursor)
        next_page = self.pages.pop(0)
        if isinstance(next_page, Exception):
            raise next_page
        return next_page

    def cancel_measurement(self, measurement_id):
        self.cancel_calls += 1
        raise AssertionError("watching must never cancel bench execution")


def test_watch_advances_cursor_without_duplicate_events_until_terminal_state() -> None:
    client = FakeWatchClient(
        summaries=[
            _summary(MeasurementState.RUNNING),
            _summary(MeasurementState.RUNNING, completed=1),
            _summary(MeasurementState.PASSED, completed=2),
        ],
        pages=[
            _page(_event(1, "one\n"), _event(2, "two\n")),
            _page(_event(3, "three\n")),
            _page(_event(4, "final\n")),
        ],
    )
    state = MeasurementWatchState(MEASUREMENT_ID)
    summaries = []
    delivered_cursors = []
    sleeps = []

    returned = watch_measurement(
        client,
        state,
        poll_interval_s=0.25,
        on_summary=summaries.append,
        on_events=lambda page: delivered_cursors.extend(
            event.cursor.value for event in page.events
        ),
        sleep=sleeps.append,
    )

    assert returned is state
    assert state.cursor == EventCursor(4)
    assert state.summary is not None
    assert state.summary.state is MeasurementState.PASSED
    assert client.requested_cursors == [EventCursor(0), EventCursor(2), EventCursor(3)]
    assert delivered_cursors == [1, 2, 3, 4]
    assert [summary.state for summary in summaries] == [
        MeasurementState.RUNNING,
        MeasurementState.RUNNING,
        MeasurementState.PASSED,
    ]
    assert sleeps == [0.25, 0.25]
    assert client.cancel_calls == 0


def test_watch_state_resumes_from_last_cursor_after_connection_failure() -> None:
    state = MeasurementWatchState(MEASUREMENT_ID)
    disconnected_client = FakeWatchClient(
        summaries=[
            _summary(MeasurementState.RUNNING),
            ConnectionError("client disconnected"),
        ],
        pages=[_page(_event(1, "before disconnect\n"))],
    )

    with pytest.raises(ConnectionError, match="disconnected"):
        watch_measurement(
            disconnected_client,
            state,
            poll_interval_s=0,
            sleep=lambda interval: None,
        )

    assert state.cursor == EventCursor(1)
    assert disconnected_client.cancel_calls == 0

    reconnected_client = FakeWatchClient(
        summaries=[_summary(MeasurementState.PASSED, completed=2)],
        pages=[_page(_event(2, "after reconnect\n"))],
    )
    delivered = []

    watch_measurement(
        reconnected_client,
        state,
        on_events=lambda page: delivered.extend(event.cursor.value for event in page.events),
        sleep=lambda interval: None,
    )

    assert reconnected_client.requested_cursors == [EventCursor(1)]
    assert delivered == [2]
    assert state.cursor == EventCursor(2)
    assert state.is_terminal
    assert reconnected_client.cancel_calls == 0


def test_watch_signals_truncation_before_delivering_retained_events() -> None:
    client = FakeWatchClient(
        summaries=[_summary(MeasurementState.PASSED, completed=2)],
        pages=[
            _page(
                _event(10, "oldest retained\n"),
                oldest_retained_cursor=10,
                truncated=True,
            )
        ],
    )
    state = MeasurementWatchState(MEASUREMENT_ID)
    callbacks = []

    watch_measurement(
        client,
        state,
        on_truncation=lambda cursor: callbacks.append(("truncated", cursor.value)),
        on_events=lambda page: callbacks.append(
            ("events", [event.cursor.value for event in page.events])
        ),
        sleep=lambda interval: None,
    )

    assert callbacks == [("truncated", 10), ("events", [10])]
    assert state.cursor == EventCursor(10)


def test_terminal_summary_still_flushes_final_event_page_before_return() -> None:
    client = FakeWatchClient(
        summaries=[_summary(MeasurementState.FAILED, completed=2)],
        pages=[_page(_event(1, "final failure output\n"))],
    )
    callbacks = []

    state = watch_measurement(
        client,
        MeasurementWatchState(MEASUREMENT_ID),
        on_summary=lambda summary: callbacks.append(("summary", summary.state.value)),
        on_events=lambda page: callbacks.append(("events", page.next_cursor.value)),
        sleep=lambda interval: None,
    )

    assert callbacks == [("summary", "failed"), ("events", 1)]
    assert state.cursor == EventCursor(1)


def test_poll_updates_summary_even_when_no_new_events_are_available() -> None:
    client = FakeWatchClient(
        summaries=[_summary(MeasurementState.RUNNING, completed=1)],
        pages=[_page(next_cursor=3, oldest_retained_cursor=1)],
    )
    state = MeasurementWatchState(MEASUREMENT_ID, cursor=EventCursor(3))

    update = poll_measurement_watch(client, state)

    assert update.summary.counts.completed == 1
    assert update.event_page.events == ()
    assert state.summary == update.summary
    assert state.cursor == EventCursor(3)


def test_watch_rejects_invalid_state_and_poll_interval() -> None:
    with pytest.raises(ValueError, match="measurement id"):
        MeasurementWatchState("")

    client = FakeWatchClient([], [])
    with pytest.raises(ValueError, match="poll interval"):
        watch_measurement(
            client,
            MeasurementWatchState(MEASUREMENT_ID),
            poll_interval_s=-0.1,
        )

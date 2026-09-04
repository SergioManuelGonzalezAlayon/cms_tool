from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import cms_client
from cms_client import (
    MeasurementWatchState,
    _print_measurement_summary,
    _print_verbose_event_page,
    _resolve_watch_measurement_id,
    build_cli_parser,
)
from cms_protocol import (
    Artifacts,
    EventCursor,
    MeasurementEvent,
    MeasurementEventPage,
    MeasurementResult,
    MeasurementState,
    MeasurementSummary,
    TestCaseIdentity as CaseIdentity,
    TestCounts as Counts,
)


MEASUREMENT_ID = "measurement-123"


def _terminal_watch_state(
    state: MeasurementState = MeasurementState.PASSED,
) -> MeasurementWatchState:
    watch_state = MeasurementWatchState(MEASUREMENT_ID)
    watch_state.summary = MeasurementSummary(
        measurement_id=MEASUREMENT_ID,
        state=state,
        counts=Counts(),
    )
    return watch_state


def test_cli_accepts_explicit_or_active_watch_and_explicit_artifacts() -> None:
    parser = build_cli_parser()

    explicit = parser.parse_args(
        ["watch", MEASUREMENT_ID, "--cursor", "17", "--verbose", "--poll-interval", "2.5"]
    )
    active = parser.parse_args(["watch"])
    artifacts = parser.parse_args(["artifacts", MEASUREMENT_ID, "--out-dir", "downloads"])
    submitted = parser.parse_args(["test", "--mode", "default", "--feature", "beacon", "--verbose"])

    assert explicit.measurement_id == MEASUREMENT_ID
    assert explicit.cursor == 17
    assert explicit.verbose is True
    assert explicit.poll_interval == 2.5
    assert active.measurement_id == ""
    assert artifacts.measurement_id == MEASUREMENT_ID
    assert artifacts.out_dir == "downloads"
    assert submitted.verbose is True
    assert submitted.poll_interval == 1.0


def test_watch_resolution_prefers_explicit_id_and_discovers_active_measurement() -> None:
    class Client:
        def __init__(self, active):
            self.active = active
            self.discovery_calls = 0

        def get_active_measurement_id(self):
            self.discovery_calls += 1
            return self.active

    client = Client("active-measurement")

    assert _resolve_watch_measurement_id(client, "explicit-measurement") == "explicit-measurement"
    assert client.discovery_calls == 0
    assert _resolve_watch_measurement_id(client, "") == "active-measurement"
    assert client.discovery_calls == 1

    with pytest.raises(RuntimeError, match="no active measurement"):
        _resolve_watch_measurement_id(Client(None), "")


def test_concise_summary_shows_current_counts_and_failed_identities(capsys) -> None:
    failed = CaseIdentity(
        job_id="job-1",
        ordinal=1,
        total_jobs=3,
        feature="beacon",
        config="beacon/failing.json",
    )
    current = CaseIdentity(
        job_id="job-2",
        ordinal=2,
        total_jobs=3,
        feature="beacon",
        config="beacon/current.json",
    )
    summary = MeasurementSummary(
        measurement_id=MEASUREMENT_ID,
        state=MeasurementState.RUNNING,
        counts=Counts(total=3, completed=1, failed=1),
        current_test=current,
        failed_tests=(failed,),
    )

    _print_measurement_summary(summary)

    output = capsys.readouterr().out
    assert f"{MEASUREMENT_ID}: running" in output
    assert "total=3 completed=1 passed=0 failed=1 skipped=0 remaining=2" in output
    assert "current: 2/3 beacon beacon/current.json" in output
    assert "failed tests:" in output
    assert "1/3 beacon beacon/failing.json" in output


def test_verbose_display_preserves_console_source_and_lifecycle(capsys) -> None:
    timestamp = datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc)
    events = (
        MeasurementEvent(
            measurement_id=MEASUREMENT_ID,
            cursor=EventCursor(1),
            timestamp=timestamp,
            event_type="console",
            details={"source": "stdout", "text": "ready\n"},
        ),
        MeasurementEvent(
            measurement_id=MEASUREMENT_ID,
            cursor=EventCursor(2),
            timestamp=timestamp,
            event_type="console",
            details={"source": "stderr", "text": "warning\n"},
        ),
        MeasurementEvent(
            measurement_id=MEASUREMENT_ID,
            cursor=EventCursor(3),
            timestamp=timestamp,
            event_type="lifecycle",
            details={"state": "process_completed"},
        ),
    )
    page = MeasurementEventPage(
        measurement_id=MEASUREMENT_ID,
        events=events,
        next_cursor=EventCursor(3),
        oldest_retained_cursor=EventCursor(1),
    )

    _print_verbose_event_page(page)

    output = capsys.readouterr().out
    assert "[stdout] ready" in output
    assert "[stderr] warning" in output
    assert "[event] process_completed" in output
    assert timestamp.isoformat() in output


def test_watch_command_discovers_active_measurement_before_attaching(monkeypatch) -> None:
    class Client:
        def get_active_measurement_id(self):
            return MEASUREMENT_ID

    client = Client()
    attached = []
    monkeypatch.setattr(cms_client, "CMSClient", lambda **kwargs: client)
    monkeypatch.setattr(
        cms_client,
        "_watch_remote_measurement",
        lambda client, measurement_id, **kwargs: (
            attached.append((measurement_id, kwargs)) or _terminal_watch_state()
        ),
    )

    exit_code = cms_client.main(["watch"])

    assert exit_code == 0
    assert attached[0][0] == MEASUREMENT_ID
    assert attached[0][1]["after_cursor"] == EventCursor(0)


def test_test_command_reuses_watch_and_downloads_by_submitted_measurement_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Client:
        def __init__(self):
            self.downloads = []

        def run_test(self, *args, **kwargs):
            return MeasurementResult(
                measurement_id=MEASUREMENT_ID,
                state=MeasurementState.ACCEPTED,
            )

        def get_measurement(self, measurement_id):
            assert measurement_id == MEASUREMENT_ID
            return MeasurementResult(
                measurement_id=measurement_id,
                state=MeasurementState.PASSED,
                artifacts=[Artifacts(name="result.json", url="/result.json")],
            )

        def download_measurement_artifacts(self, measurement_id, out_dir):
            self.downloads.append((measurement_id, out_dir))
            return [out_dir / "result.json"]

    client = Client()
    watched = []
    monkeypatch.setattr(cms_client, "CMSClient", lambda **kwargs: client)
    monkeypatch.setattr(
        cms_client,
        "_watch_remote_measurement",
        lambda client, measurement_id, **kwargs: (
            watched.append((measurement_id, kwargs)) or _terminal_watch_state()
        ),
    )

    exit_code = cms_client.main(
        ["test", "--mode", "default", "--feature", "beacon", "--out-dir", str(tmp_path)]
    )

    assert exit_code == 0
    assert watched[0][0] == MEASUREMENT_ID
    assert client.downloads == [(MEASUREMENT_ID, tmp_path)]


def test_all_feature_selection_submits_one_campaign(monkeypatch) -> None:
    class Client:
        def __init__(self):
            self.submitted_features = []

        def run_test(self, mode, feature, **kwargs):
            self.submitted_features.append(feature)
            return MeasurementResult(
                measurement_id="measurement-all",
                state=MeasurementState.ACCEPTED,
            )

        def get_measurement(self, measurement_id):
            return MeasurementResult(measurement_id=measurement_id, state=MeasurementState.PASSED)

    client = Client()
    watched = []
    monkeypatch.setattr(cms_client, "CMSClient", lambda **kwargs: client)
    monkeypatch.setattr(
        cms_client,
        "_watch_remote_measurement",
        lambda client, measurement_id, **kwargs: (
            watched.append(measurement_id) or _terminal_watch_state()
        ),
    )

    exit_code = cms_client.main(["test", "--mode", "default", "--feature", "all"])

    assert exit_code == 0
    assert client.submitted_features == ["all"]
    assert watched == ["measurement-all"]


def test_artifacts_command_never_uses_last_measurement(tmp_path: Path, monkeypatch) -> None:
    class Client:
        def __init__(self):
            self.downloads = []

        def download_measurement_artifacts(self, measurement_id, out_dir):
            self.downloads.append((measurement_id, out_dir))
            return [out_dir / "result.json"]

        def download_last_artifacts(self, out_dir):
            raise AssertionError("explicit artifact command must not use last")

    client = Client()
    monkeypatch.setattr(cms_client, "CMSClient", lambda **kwargs: client)

    exit_code = cms_client.main(
        ["artifacts", MEASUREMENT_ID, "--out-dir", str(tmp_path)]
    )

    assert exit_code == 0
    assert client.downloads == [(MEASUREMENT_ID, tmp_path)]

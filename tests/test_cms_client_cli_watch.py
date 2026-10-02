from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import cms_client
from cms_client import (
    MeasurementWatchState,
    _print_measurement_summary,
    _print_verbose_event_page,
    _resolve_watch_measurement_id,
    _watch_remote_measurement,
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
    reconnect = parser.parse_args(["watch", "--reconnect"])
    artifacts = parser.parse_args(["artifacts", MEASUREMENT_ID, "--out-dir", "downloads"])
    submitted = parser.parse_args(["test", "--mode", "default", "--feature", "beacon", "--verbose"])

    assert explicit.measurement_id == MEASUREMENT_ID
    assert explicit.cursor == 17
    assert explicit.verbose is True
    assert explicit.poll_interval == 2.5
    assert active.measurement_id == ""
    assert reconnect.reconnect is True
    assert artifacts.measurement_id == MEASUREMENT_ID
    assert artifacts.out_dir == "downloads"
    assert submitted.verbose is True
    assert submitted.poll_interval == 1.0


def test_cli_accepts_artifact_recovery_commands() -> None:
    parser = build_cli_parser()

    listed = parser.parse_args(["ARTIFACTS", "--list"])
    all_debug = parser.parse_args(["ARTIFACTS", "--debug", "--get", "all"])
    one_debug = parser.parse_args(
        ["ARTIFACTS", "--debug", "--get", MEASUREMENT_ID]
    )
    protocol_results = parser.parse_args(["ARTIFACTS", "--get-results", "ble"])

    assert listed.list is True
    assert all_debug.debug is True
    assert all_debug.get == "all"
    assert one_debug.get == MEASUREMENT_ID
    assert protocol_results.get_results == "ble"


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


def test_watch_reconnect_resolves_only_the_persisted_measurement_id(tmp_path: Path) -> None:
    config_path = tmp_path / "cms_client_config.json"
    config_path.write_text(
        json.dumps(
            {
                "last_measurement": {
                    "measurement_id": "reconnect-measurement-001",
                    "mode": "diff",
                    "endpoint": "http://bench.example",
                }
            }
        ),
        encoding="utf-8",
    )

    class Client:
        def get_active_measurement_id(self):
            raise AssertionError("reconnect must not discover an active measurement")

    assert _resolve_watch_measurement_id(
        Client(),
        "",
        reconnect=True,
        config_path=config_path,
    ) == "reconnect-measurement-001"


def test_watch_reconnect_rejects_missing_record_and_explicit_measurement_id(tmp_path: Path) -> None:
    class Client:
        def get_active_measurement_id(self):
            return None

    with pytest.raises(RuntimeError, match="recovery record does not exist"):
        _resolve_watch_measurement_id(
            Client(),
            "",
            reconnect=True,
            config_path=tmp_path / "missing.json",
        )
    with pytest.raises(RuntimeError, match="cannot be combined"):
        _resolve_watch_measurement_id(
            Client(),
            "explicit-measurement",
            reconnect=True,
            config_path=tmp_path / "missing.json",
        )


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


def test_remote_watch_requests_live_events_only_with_verbose(monkeypatch) -> None:
    callbacks = []

    def fake_watch_measurement(client, state, **kwargs):
        callbacks.append(kwargs["on_events"])
        return state

    monkeypatch.setattr(cms_client, "watch_measurement", fake_watch_measurement)

    _watch_remote_measurement(object(), MEASUREMENT_ID, verbose=False)
    _watch_remote_measurement(object(), MEASUREMENT_ID, verbose=True)

    assert callbacks[0] is None
    assert callbacks[1] is not None


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

        def download_measurement_artifacts(
            self,
            measurement_id,
            out_dir,
            *,
            debug_persisted=False,
        ):
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

        def download_measurement_artifacts(
            self,
            measurement_id,
            out_dir,
            *,
            debug_persisted=False,
        ):
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


def test_artifact_recovery_commands_dispatch_to_expected_client_methods(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Client:
        def __init__(self):
            self.calls = []

        def list_saved_measurements(self):
            self.calls.append(("list",))
            return []

        def download_bench_artifacts(self, scope, out_dir):
            self.calls.append(("scope", scope, out_dir))
            return [out_dir / scope / "artifact.log"]

        def download_debug_measurement_tree(self, measurement_id, out_dir):
            self.calls.append(("measurement", measurement_id, out_dir))
            return [out_dir / "cmsd" / "measurements" / measurement_id / "pytest.log"]

    client = Client()
    monkeypatch.setattr(cms_client, "CMSClient", lambda **kwargs: client)

    assert cms_client.main(["ARTIFACTS", "--list"]) == 0
    assert cms_client.main(
        ["ARTIFACTS", "--debug", "--get", "all", "--out-dir", str(tmp_path)]
    ) == 0
    assert cms_client.main(
        ["ARTIFACTS", "--debug", "--get", MEASUREMENT_ID, "--out-dir", str(tmp_path)]
    ) == 0
    assert cms_client.main(
        ["ARTIFACTS", "--get-results", "ble", "--out-dir", str(tmp_path)]
    ) == 0

    assert client.calls == [
        ("list",),
        ("scope", "cmsd", tmp_path),
        ("scope", "orchestrator", tmp_path),
        ("measurement", MEASUREMENT_ID, tmp_path),
        ("scope", "ble", tmp_path),
    ]

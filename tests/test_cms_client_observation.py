from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cms_client import CMSClient, CMSProtocolMismatchError
from cms_protocol import (
    CMS_OBSERVATION_PROTOCOL_VERSION,
    Artifacts,
    EventCursor,
    MeasurementEvent,
    MeasurementEventPage,
    MeasurementState,
    MeasurementSummary,
    TestCounts as Counts,
    artifact_to_dict,
    measurement_event_page_to_dict,
    measurement_summary_to_dict,
)


MEASUREMENT_ID = "measurement-123"


def _client(fake_request) -> CMSClient:
    client = CMSClient.__new__(CMSClient)
    client.base_url = "http://cms-bench:8000"
    client.api_token = ""
    client.timeout_s = 30.0
    client._request_json = fake_request
    return client


def test_client_discovers_active_measurement_with_protocol_version() -> None:
    requests = []

    def request(method, path, payload=None):
        requests.append((method, path, payload))
        return {
            "protocol_version": CMS_OBSERVATION_PROTOCOL_VERSION,
            "measurement_id": MEASUREMENT_ID,
        }

    client = _client(request)

    assert client.get_active_measurement_id() == MEASUREMENT_ID
    assert requests == [
        (
            "GET",
            f"/measurement/active?protocol_version={CMS_OBSERVATION_PROTOCOL_VERSION}",
            None,
        )
    ]


def test_client_active_discovery_returns_none_when_bench_is_idle() -> None:
    client = _client(
        lambda method, path: {
            "protocol_version": CMS_OBSERVATION_PROTOCOL_VERSION,
            "measurement_id": None,
        }
    )

    assert client.get_active_measurement_id() is None


def test_client_retrieves_typed_measurement_summary() -> None:
    expected = MeasurementSummary(
        measurement_id=MEASUREMENT_ID,
        state=MeasurementState.RUNNING,
        counts=Counts(total=2, completed=1, passed=1),
    )
    requested_paths = []

    def request(method, path):
        requested_paths.append(path)
        return measurement_summary_to_dict(expected)

    client = _client(request)

    summary = client.get_measurement_summary(MEASUREMENT_ID)

    assert summary == expected
    assert requested_paths == [
        f"/measurement/{MEASUREMENT_ID}/summary?protocol_version="
        f"{CMS_OBSERVATION_PROTOCOL_VERSION}"
    ]


def test_client_retrieves_events_strictly_after_acknowledged_cursor() -> None:
    event = MeasurementEvent(
        measurement_id=MEASUREMENT_ID,
        cursor=EventCursor(3),
        timestamp=datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc),
        event_type="console",
        details={"source": "stdout", "text": "ready\n"},
    )
    expected = MeasurementEventPage(
        measurement_id=MEASUREMENT_ID,
        events=(event,),
        next_cursor=EventCursor(3),
        oldest_retained_cursor=EventCursor(1),
    )
    requested_paths = []

    def request(method, path):
        requested_paths.append(path)
        return measurement_event_page_to_dict(expected)

    client = _client(request)

    page = client.get_measurement_events(MEASUREMENT_ID, EventCursor(2))

    assert page == expected
    assert requested_paths == [
        f"/measurement/{MEASUREMENT_ID}/events?after=2&protocol_version="
        f"{CMS_OBSERVATION_PROTOCOL_VERSION}"
    ]


@pytest.mark.parametrize("daemon_version", [0, CMS_OBSERVATION_PROTOCOL_VERSION + 1])
def test_client_rejects_missing_or_incompatible_observation_protocol(daemon_version: int) -> None:
    client = _client(
        lambda method, path: {
            "protocol_version": daemon_version,
            "measurement_id": None,
        }
    )

    with pytest.raises(CMSProtocolMismatchError, match="protocol mismatch"):
        client.get_active_measurement_id()


def test_client_rejects_mismatched_measurement_and_invalid_event_cursor_progress() -> None:
    wrong_summary = MeasurementSummary(
        measurement_id="other-measurement",
        state=MeasurementState.RUNNING,
        counts=Counts(),
    )
    client = _client(lambda method, path: measurement_summary_to_dict(wrong_summary))

    with pytest.raises(CMSProtocolMismatchError, match="does not match"):
        client.get_measurement_summary(MEASUREMENT_ID)

    repeated_event = MeasurementEvent(
        measurement_id=MEASUREMENT_ID,
        cursor=EventCursor(2),
        timestamp=datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc),
        event_type="lifecycle",
        details={"state": "process_started"},
    )
    repeated_page = MeasurementEventPage(
        measurement_id=MEASUREMENT_ID,
        events=(repeated_event,),
        next_cursor=EventCursor(2),
        oldest_retained_cursor=EventCursor(1),
    )
    client = _client(lambda method, path: measurement_event_page_to_dict(repeated_page))

    with pytest.raises(CMSProtocolMismatchError, match="repeated"):
        client.get_measurement_events(MEASUREMENT_ID, EventCursor(2))

    backwards_page = MeasurementEventPage(
        measurement_id=MEASUREMENT_ID,
        events=(),
        next_cursor=EventCursor(1),
        oldest_retained_cursor=EventCursor(1),
    )
    client = _client(lambda method, path: measurement_event_page_to_dict(backwards_page))

    with pytest.raises(CMSProtocolMismatchError, match="backwards"):
        client.get_measurement_events(MEASUREMENT_ID, EventCursor(2))


def test_client_lists_and_downloads_artifacts_by_explicit_measurement_id(tmp_path: Path) -> None:
    source = tmp_path / "published" / "result.json"
    source.parent.mkdir()
    source.write_text('{"passed":true}\n', encoding="utf-8")
    artifact = Artifacts(name="results/result.json", path=source)
    requests = []

    def request(method, path):
        requests.append(path)
        return {
            "protocol_version": CMS_OBSERVATION_PROTOCOL_VERSION,
            "measurement_id": MEASUREMENT_ID,
            "artifacts": [artifact_to_dict(artifact)],
        }

    client = _client(request)

    listed = client.list_measurement_artifacts(MEASUREMENT_ID)
    saved = client.download_measurement_artifacts(MEASUREMENT_ID, tmp_path / "downloads")

    assert listed == [artifact]
    assert saved == [tmp_path / "downloads" / "results" / "result.json"]
    assert saved[0].read_text(encoding="utf-8") == '{"passed":true}\n'
    assert requests == [
        f"/measurement/{MEASUREMENT_ID}/artifacts?storage=runtime&protocol_version="
        f"{CMS_OBSERVATION_PROTOCOL_VERSION}",
        f"/measurement/{MEASUREMENT_ID}/artifacts?storage=runtime&protocol_version="
        f"{CMS_OBSERVATION_PROTOCOL_VERSION}",
    ]
    assert "/artifacts/last" not in requests


def test_client_explicit_artifact_download_rejects_empty_list(tmp_path: Path) -> None:
    client = _client(
        lambda method, path: {
            "protocol_version": CMS_OBSERVATION_PROTOCOL_VERSION,
            "measurement_id": MEASUREMENT_ID,
            "artifacts": [],
        }
    )

    with pytest.raises(RuntimeError, match="no published artifacts"):
        client.download_measurement_artifacts(MEASUREMENT_ID, tmp_path)


def test_client_lists_saved_measurements_and_downloads_bench_scopes(tmp_path: Path) -> None:
    scope_source = tmp_path / "source" / "ble-results.csv"
    scope_source.parent.mkdir()
    scope_source.write_text("result\n", encoding="utf-8")
    debug_source = tmp_path / "source" / "test_stdout.log"
    debug_source.write_text("test output\n", encoding="utf-8")
    requests = []

    def request(method, path):
        requests.append(path)
        if path.startswith("/artifacts/measurements?"):
            return {
                "protocol_version": CMS_OBSERVATION_PROTOCOL_VERSION,
                "measurements": [
                    {
                        "measurement_id": MEASUREMENT_ID,
                        "state": "",
                        "has_manifest": False,
                        "artifact_count": 1,
                        "last_modified_at": "2026-09-22T18:06:42+00:00",
                    }
                ],
            }
        if path.startswith("/bench/artifacts/ble?"):
            return {
                "protocol_version": CMS_OBSERVATION_PROTOCOL_VERSION,
                "scope": "ble",
                "artifacts": [
                    {
                        "name": "ble-results.csv",
                        "path": str(scope_source),
                        "url": "/bench/artifacts/ble/ble-results.csv",
                    }
                ],
            }
        if path.startswith(f"/measurement/{MEASUREMENT_ID}/artifacts?"):
            return {
                "protocol_version": CMS_OBSERVATION_PROTOCOL_VERSION,
                "measurement_id": MEASUREMENT_ID,
                "artifacts": [
                    {
                        "name": "test_stdout.log",
                        "path": str(debug_source),
                        "url": f"/measurement/{MEASUREMENT_ID}/artifacts/test_stdout.log?storage=debug",
                    }
                ],
            }
        raise AssertionError(f"unexpected request: {path}")

    client = _client(request)

    assert client.list_saved_measurements()[0]["measurement_id"] == MEASUREMENT_ID
    scope_downloads = client.download_bench_artifacts("ble", tmp_path / "downloads")
    debug_downloads = client.download_debug_measurement_tree(
        MEASUREMENT_ID,
        tmp_path / "downloads",
    )

    assert scope_downloads == [tmp_path / "downloads" / "ble" / "ble-results.csv"]
    assert debug_downloads == [
        tmp_path / "downloads" / "cmsd" / "measurements" / MEASUREMENT_ID / "test_stdout.log"
    ]
    assert scope_downloads[0].read_text(encoding="utf-8") == "result\n"
    assert debug_downloads[0].read_text(encoding="utf-8") == "test output\n"
    assert requests == [
        f"/artifacts/measurements?protocol_version={CMS_OBSERVATION_PROTOCOL_VERSION}",
        f"/bench/artifacts/ble?protocol_version={CMS_OBSERVATION_PROTOCOL_VERSION}",
        f"/measurement/{MEASUREMENT_ID}/artifacts?storage=debug&protocol_version="
        f"{CMS_OBSERVATION_PROTOCOL_VERSION}",
    ]

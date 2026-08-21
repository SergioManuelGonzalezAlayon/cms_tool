from __future__ import annotations

import io
import urllib.error

import pytest

import cms_client
from cms_client import (
    CMSCLIExitCode,
    CMSClient,
    CMSConnectionError,
    CMSDaemonError,
    CMSProtocolMismatchError,
    _measurement_exit_code,
)
from cms_protocol import MeasurementState


def _client() -> CMSClient:
    client = CMSClient.__new__(CMSClient)
    client.base_url = "http://cms-bench:8000"
    client.api_token = ""
    client.timeout_s = 1.0
    return client


def test_cli_exit_code_values_are_stable() -> None:
    assert {name: int(code) for name, code in CMSCLIExitCode.__members__.items()} == {
        "SUCCESS": 0,
        "TEST_FAILED": 1,
        "USAGE_ERROR": 2,
        "TEST_CANCELLED": 3,
        "TEST_REJECTED": 4,
        "CONNECTION_FAILURE": 5,
        "PROTOCOL_MISMATCH": 6,
        "CLIENT_ERROR": 7,
    }


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (MeasurementState.PASSED, CMSCLIExitCode.SUCCESS),
        (MeasurementState.FAILED, CMSCLIExitCode.TEST_FAILED),
        (MeasurementState.CANCELLED, CMSCLIExitCode.TEST_CANCELLED),
        (MeasurementState.REJECTED, CMSCLIExitCode.TEST_REJECTED),
        (MeasurementState.RUNNING, CMSCLIExitCode.CLIENT_ERROR),
        (None, CMSCLIExitCode.CLIENT_ERROR),
    ],
)
def test_terminal_measurement_states_map_to_distinct_exit_codes(state, expected) -> None:
    assert _measurement_exit_code(state) is expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CMSConnectionError("daemon unavailable"), CMSCLIExitCode.CONNECTION_FAILURE),
        (CMSProtocolMismatchError("version mismatch"), CMSCLIExitCode.PROTOCOL_MISMATCH),
        (CMSDaemonError(503, "bench unavailable"), CMSCLIExitCode.CLIENT_ERROR),
    ],
)
def test_main_classifies_client_failures_without_reporting_test_failure(
    error,
    expected,
    monkeypatch,
    capsys,
) -> None:
    class Client:
        def get_status(self):
            raise error

    monkeypatch.setattr(cms_client, "CMSClient", lambda **kwargs: Client())

    exit_code = cms_client.main(["status"])

    assert exit_code == int(expected)
    assert exit_code != int(CMSCLIExitCode.TEST_FAILED)
    assert str(error) in capsys.readouterr().err


def test_request_json_classifies_connection_failure(monkeypatch) -> None:
    def unavailable(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cms_client.urllib.request, "urlopen", unavailable)

    with pytest.raises(CMSConnectionError, match="connection refused"):
        _client()._request_json("GET", "/health")


def test_request_json_classifies_daemon_and_protocol_http_errors(monkeypatch) -> None:
    errors = iter(
        [
            urllib.error.HTTPError(
                "http://cms-bench:8000/measurement/active",
                409,
                "Conflict",
                hdrs=None,
                fp=io.BytesIO(
                    b'{"detail":{"code":"incompatible_protocol","message":"unsupported"}}'
                ),
            ),
            urllib.error.HTTPError(
                "http://cms-bench:8000/measurement/unknown/summary",
                404,
                "Not Found",
                hdrs=None,
                fp=io.BytesIO(
                    b'{"detail":{"code":"unknown_measurement","message":"unknown"}}'
                ),
            ),
        ]
    )

    def fail(request, timeout):
        raise next(errors)

    monkeypatch.setattr(cms_client.urllib.request, "urlopen", fail)
    client = _client()

    with pytest.raises(CMSProtocolMismatchError, match="rejected"):
        client._request_json("GET", "/measurement/active")
    with pytest.raises(CMSDaemonError) as daemon_error:
        client._request_json("GET", "/measurement/unknown/summary")
    assert daemon_error.value.status_code == 404
    assert "unknown_measurement" in daemon_error.value.detail


def test_request_json_classifies_invalid_json_as_protocol_failure(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"not-json"

    monkeypatch.setattr(
        cms_client.urllib.request,
        "urlopen",
        lambda request, timeout: Response(),
    )

    with pytest.raises(CMSProtocolMismatchError, match="invalid JSON"):
        _client()._request_json("GET", "/health")


@pytest.mark.parametrize(
    "argv",
    [
        ["watch", "measurement-123", "--cursor", "-1"],
        ["watch", "measurement-123", "--poll-interval", "-0.1"],
    ],
)
def test_invalid_watch_arguments_keep_argparse_usage_exit_code(argv) -> None:
    with pytest.raises(SystemExit) as usage_error:
        cms_client.main(argv)
    assert usage_error.value.code == int(CMSCLIExitCode.USAGE_ERROR)

from datetime import datetime, timezone

from cms_client import CMSClient, _print_measurement_result, build_cli_parser
from cms_protocol import (
    MeasurementIdentity,
    MeasurementMode,
    MeasurementResult,
    MeasurementState,
)


def test_test_command_accepts_run_name():
    parser = build_cli_parser()

    named = parser.parse_args(["test", "default", "beacon", "--run-name", "Nightly Run"])

    assert named.run_name == "Nightly Run"


def test_run_test_sends_requested_run_name_to_daemon():
    captured = {}
    client = CMSClient.__new__(CMSClient)

    def fake_request(method, path, payload):
        captured.update(payload)
        return {"measurement_id": "generated-id", "state": "accepted"}

    client._request_json = fake_request

    result = client.run_test(
        MeasurementMode.DEFAULT,
        "beacon",
        run_name="Nightly Beacon Run",
    )

    assert captured["run_name"] == "Nightly Beacon Run"
    assert "request_id" not in captured
    assert result.measurement_id == "generated-id"


def test_measurement_display_includes_run_identity(capsys):
    identity = MeasurementIdentity(
        measurement_id="beacon-20260818T093000000000Z-abc123",
        run_name="Nightly Beacon Run",
        created_at=datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc),
    )

    _print_measurement_result(
        MeasurementResult(
            measurement_id=identity.measurement_id,
            state=MeasurementState.RUNNING,
            identity=identity,
        )
    )

    output = capsys.readouterr().out
    assert "run_name: Nightly Beacon Run" in output
    assert "created_at: 2026-08-18T09:30:00+00:00" in output

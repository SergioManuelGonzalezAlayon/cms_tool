"""Client-side helpers for talking to one CMS bench daemon.

`cms_client.py` is the remote control surface used by developers, CI, or other
automation. It speaks the daemon protocol and leaves bench-local hardware
control to `cmsd.py` and `cms_bench.py`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable

from cms_protocol import (
    CMS_OBSERVATION_PROTOCOL_VERSION,
    Artifacts,
    BenchHealthReport,
    BenchIdentity,
    BenchState,
    BenchStatus,
    FirmwareImage,
    FirmwareData,
    EventCursor,
    MeasurementEventPage,
    MeasurementMode,
    MeasurementRequest,
    MeasurementResult,
    MeasurementState,
    MeasurementSummary,
    SuiteSelector,
    artifact_from_dict,
    artifact_to_dict,
    bench_health_report_from_dict,
    measurement_request_from_dict,
    measurement_request_to_dict,
    measurement_result_from_dict,
    measurement_event_page_from_dict,
    measurement_summary_from_dict,
)

CMS_CLIENT_CONFIG = "cms_client_config.json"
TERMINAL_MEASUREMENT_STATES = frozenset(
    {
        MeasurementState.PASSED,
        MeasurementState.FAILED,
        MeasurementState.CANCELLED,
        MeasurementState.REJECTED,
    }
)


class CMSCLIExitCode(IntEnum):
    """Stable process outcomes for scripts, developers, and future CI."""

    SUCCESS = 0
    TEST_FAILED = 1
    USAGE_ERROR = 2
    TEST_CANCELLED = 3
    TEST_REJECTED = 4
    CONNECTION_FAILURE = 5
    PROTOCOL_MISMATCH = 6
    CLIENT_ERROR = 7


class CMSClientError(RuntimeError):
    """Base class for classified CMS Client failures."""


class CMSConnectionError(CMSClientError):
    """The daemon could not be reached or stopped responding."""


class CMSProtocolMismatchError(CMSClientError):
    """The daemon response is incompatible with the client protocol."""


class CMSDaemonError(CMSClientError):
    """The daemon rejected or could not complete a valid client request."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"CMS daemon returned HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass(slots=True)
class CMSClientConfig:
    """Configuration needed for one remote CMS daemon client."""

    base_url: str = ""
    api_token: str = ""
    timeout_s: float = 30.0
    target: dict[str, str] | None = None
    build: dict[str, str] | None = None
    tools: dict[str, str] | None = None


@dataclass(slots=True)
class MeasurementWatchState:
    """Reconnectable client-side cursor and latest structured summary."""

    measurement_id: str
    cursor: EventCursor = EventCursor()
    summary: MeasurementSummary | None = None

    def __post_init__(self) -> None:
        if not self.measurement_id:
            raise ValueError("measurement watch requires a measurement id")

    @property
    def is_terminal(self) -> bool:
        return (
            self.summary is not None
            and self.summary.state in TERMINAL_MEASUREMENT_STATES
        )


@dataclass(frozen=True, slots=True)
class MeasurementWatchUpdate:
    """One independently fetched summary and cursor-based event page."""

    requested_cursor: EventCursor
    summary: MeasurementSummary
    event_page: MeasurementEventPage
    console_truncated: bool = False


class CMSClient:
    """Remote client for one CMS bench daemon endpoint."""

    def __init__(
        self,
        base_url: str = "",
        api_token: str = "",
        config_path: str | Path = CMS_CLIENT_CONFIG,
    ):
        config = load_client_config(config_path)
        self.base_url = (base_url or config.base_url).rstrip("/")
        self.api_token = api_token or config.api_token
        self.timeout_s = config.timeout_s

    def get_health(self) -> BenchHealthReport:
        """Fetch the daemon health view for operator and CI checks."""
        return bench_health_report_from_dict(self._request_json("GET", "/health"))

    def get_status(self) -> BenchStatus:
        """Ask whether the bench is idle, busy, offline, or in error."""
        payload = self._request_json("GET", "/bench")
        identity_data = payload.get("identity", {})
        identity = BenchIdentity(
            bench_id=str(identity_data.get("bench_id", "")),
            board=str(identity_data.get("board", "")),
            device=str(identity_data.get("device", "")),
            hostname=str(identity_data.get("hostname", "")),
            profile=str(identity_data.get("profile", "bench-lite")),
        )
        return BenchStatus(
            identity=identity,
            state=BenchState(str(payload.get("bench_state", BenchState.ERROR.value))),
            active_measurement_id=payload.get("active_measurement_id"),
            detail=str(payload.get("startup_error", "")),
        )

    def list_tests(self) -> dict[str, Any]:
        """Return test modes and features advertised by the bench."""
        return self._request_json("GET", "/tests")

    def submit_measurement(self, request: MeasurementRequest) -> MeasurementResult:
        """Send one measurement request to the daemon and receive an accepted/busy reply."""
        payload = self._request_json("POST", "/measurement", measurement_request_to_dict(request))
        return measurement_result_from_dict(payload)

    def send_firmware(self, request: MeasurementRequest) -> MeasurementResult:
        """Stage firmware and metadata on the remote bench."""
        payload = measurement_request_to_dict(request)
        payload["image_payloads"] = _local_firmware_payloads(request)
        payload = self._request_json("POST", "/firmware", payload)
        return measurement_result_from_dict(payload)

    def run_test(
        self,
        mode: MeasurementMode,
        feature: str,
        config: str = "default",
        upload_results_to_database: bool = False,
        run_name: str = "",
    ) -> MeasurementResult:
        """Start a remote test using the most recently staged firmware."""
        config_hash = _validate_local_test_config(feature, config)
        payload = self._request_json(
            "POST",
            "/test",
            {
                "mode": mode.value,
                "feature": feature,
                "config": config,
                "config_hash": config_hash,
                "run_name": run_name,
                "upload_results_to_database": upload_results_to_database,
            },
        )
        return measurement_result_from_dict(payload)

    def get_measurement(self, measurement_id: str) -> MeasurementResult:
        """Fetch the current state and result summary for one submitted measurement."""
        return measurement_result_from_dict(self._request_json("GET", f"/measurement/{measurement_id}"))

    def get_active_measurement_id(self) -> str | None:
        """Discover the measurement currently controlled by the daemon."""

        payload = self._request_json("GET", _observation_path("/measurement/active"))
        _validate_observation_envelope(payload)
        measurement_id = payload.get("measurement_id")
        if measurement_id is None:
            return None
        if not isinstance(measurement_id, str) or not measurement_id:
            raise CMSProtocolMismatchError(
                "active measurement response contains an invalid measurement id"
            )
        return measurement_id

    def get_measurement_summary(self, measurement_id: str) -> MeasurementSummary:
        """Fetch concise authoritative test state for one measurement."""

        encoded_id = urllib.parse.quote(measurement_id, safe="")
        payload = self._request_json(
            "GET",
            _observation_path(f"/measurement/{encoded_id}/summary"),
        )
        _validate_observation_envelope(payload, measurement_id)
        try:
            return measurement_summary_from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise CMSProtocolMismatchError(
                f"measurement summary response is invalid: {exc}"
            ) from exc

    def get_measurement_events(
        self,
        measurement_id: str,
        after_cursor: EventCursor = EventCursor(),
    ) -> MeasurementEventPage:
        """Fetch retained events strictly after the supplied client cursor."""

        encoded_id = urllib.parse.quote(measurement_id, safe="")
        path = _observation_path(
            f"/measurement/{encoded_id}/events",
            after=str(after_cursor.value),
        )
        payload = self._request_json("GET", path)
        _validate_observation_envelope(payload, measurement_id)
        try:
            page = measurement_event_page_from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise CMSProtocolMismatchError(
                f"measurement event response is invalid: {exc}"
            ) from exc
        if page.next_cursor < after_cursor:
            raise CMSProtocolMismatchError(
                "measurement event response moved the cursor backwards"
            )
        if any(event.cursor <= after_cursor for event in page.events):
            raise CMSProtocolMismatchError(
                "measurement event response repeated an acknowledged cursor"
            )
        return page

    def list_measurement_artifacts(self, measurement_id: str) -> list[Artifacts]:
        """List published artifacts belonging to an explicit measurement."""

        encoded_id = urllib.parse.quote(measurement_id, safe="")
        payload = self._request_json(
            "GET",
            _observation_path(f"/measurement/{encoded_id}/artifacts"),
        )
        _validate_observation_envelope(payload, measurement_id)
        try:
            return [artifact_from_dict(artifact) for artifact in payload.get("artifacts", [])]
        except (TypeError, ValueError) as exc:
            raise CMSProtocolMismatchError(
                f"measurement artifact response is invalid: {exc}"
            ) from exc

    def cancel_measurement(self, measurement_id: str) -> MeasurementResult:
        """Request cancellation for the active measurement when the daemon allows it."""
        return measurement_result_from_dict(
            self._request_json("POST", f"/measurement/{measurement_id}/cancel")
        )

    def cancel_active_test(self) -> MeasurementResult:
        """Request cancellation for the active measurement, if any."""
        return measurement_result_from_dict(self._request_json("POST", "/cancel"))

    def download_artifact(self, measurement_id: str, artifact_name: str, out_dir: Path) -> Path:
        """Copy one published result artifact from the daemon to the client host."""
        artifacts = self.list_measurement_artifacts(measurement_id)
        artifact = next((item for item in artifacts if item.name == artifact_name), None)
        if artifact is None:
            raise RuntimeError(f"artifact is not available: {artifact_name}")

        return self._download_artifact_payload(artifact_to_dict(artifact), out_dir)

    def download_measurement_artifacts(self, measurement_id: str, out_dir: Path) -> list[Path]:
        """Download every published artifact for an explicit measurement."""

        artifacts = self.list_measurement_artifacts(measurement_id)
        if not artifacts:
            raise RuntimeError(f"measurement has no published artifacts: {measurement_id}")
        return [
            self._download_artifact_payload(artifact_to_dict(artifact), out_dir)
            for artifact in artifacts
        ]

    def download_last_artifacts(self, out_dir: Path) -> list[Path]:
        """Download every artifact from the last completed remote test."""
        payload = self._request_json("GET", "/artifacts/last")
        measurement_id = str(payload.get("measurement_id", ""))
        artifacts = payload.get("artifacts", [])
        if not measurement_id or not artifacts:
            raise RuntimeError("no completed test artifacts are available")

        saved: list[Path] = []
        for artifact in artifacts:
            name = str(artifact.get("name", ""))
            if not name:
                continue
            saved.append(self._download_artifact_payload(artifact, out_dir))
        return saved

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                response_body = response.read().decode("utf-8")
                try:
                    return json.loads(response_body)
                except json.JSONDecodeError as exc:
                    raise CMSProtocolMismatchError(
                        "CMS daemon returned invalid JSON"
                    ) from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if _http_error_code(detail) == "incompatible_protocol":
                raise CMSProtocolMismatchError(
                    f"CMS daemon rejected the observation protocol: {detail}"
                ) from exc
            raise CMSDaemonError(exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise CMSConnectionError(f"CMS daemon is unreachable: {exc.reason}") from exc
        except TimeoutError as exc:
            raise CMSConnectionError("CMS daemon request timed out") from exc

    def _download_artifact_payload(self, artifact: dict[str, Any], out_dir: Path) -> Path:
        name = str(artifact.get("name", ""))
        out_dir.mkdir(parents=True, exist_ok=True)
        destination = out_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_url = str(artifact.get("url", ""))
        source_path = str(artifact.get("path", ""))
        if source_path and Path(source_path).exists():
            shutil.copy2(source_path, destination)
            return destination
        try:
            if source_url.startswith("http://") or source_url.startswith("https://"):
                urllib.request.urlretrieve(source_url, destination)
                return destination
            if source_url.startswith("/"):
                with urllib.request.urlopen(
                    f"{self.base_url}{source_url}",
                    timeout=self.timeout_s,
                ) as response:
                    destination.write_bytes(response.read())
                return destination
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CMSDaemonError(exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise CMSConnectionError(f"CMS artifact download failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise CMSConnectionError("CMS artifact download timed out") from exc
        raise RuntimeError(f"artifact has no downloadable location: {name}")


def _observation_path(path: str, **query: str) -> str:
    parameters = {
        **query,
        "protocol_version": str(CMS_OBSERVATION_PROTOCOL_VERSION),
    }
    return f"{path}?{urllib.parse.urlencode(parameters)}"


def _validate_observation_envelope(
    payload: dict[str, Any],
    expected_measurement_id: str | None = None,
) -> None:
    try:
        protocol_version = int(payload.get("protocol_version", 0))
    except (TypeError, ValueError) as exc:
        raise CMSProtocolMismatchError(
            "observation response contains an invalid protocol version"
        ) from exc
    if protocol_version != CMS_OBSERVATION_PROTOCOL_VERSION:
        raise CMSProtocolMismatchError(
            "CMS observation protocol mismatch: "
            f"client={CMS_OBSERVATION_PROTOCOL_VERSION}, daemon={protocol_version}"
        )
    if expected_measurement_id is None:
        return
    response_measurement_id = payload.get("measurement_id")
    if response_measurement_id != expected_measurement_id:
        raise CMSProtocolMismatchError(
            "observation response measurement id does not match the requested measurement"
        )


def _http_error_code(detail: str) -> str:
    try:
        payload = json.loads(detail)
    except (TypeError, json.JSONDecodeError):
        return ""
    error_detail = payload.get("detail", {}) if isinstance(payload, dict) else {}
    return str(error_detail.get("code", "")) if isinstance(error_detail, dict) else ""


def poll_measurement_watch(
    client: CMSClient,
    state: MeasurementWatchState,
) -> MeasurementWatchUpdate:
    """Fetch one summary and one event page, advancing state only on success."""

    requested_cursor = state.cursor
    summary = client.get_measurement_summary(state.measurement_id)
    event_page = client.get_measurement_events(state.measurement_id, requested_cursor)
    console_truncated = (
        event_page.truncated
        and event_page.oldest_retained_cursor.value > 0
        and requested_cursor.value < event_page.oldest_retained_cursor.value - 1
    )
    state.summary = summary
    state.cursor = event_page.next_cursor
    return MeasurementWatchUpdate(
        requested_cursor=requested_cursor,
        summary=summary,
        event_page=event_page,
        console_truncated=console_truncated,
    )


def watch_measurement(
    client: CMSClient,
    state: MeasurementWatchState,
    *,
    poll_interval_s: float = 1.0,
    on_summary: Callable[[MeasurementSummary], None] | None = None,
    on_events: Callable[[MeasurementEventPage], None] | None = None,
    on_truncation: Callable[[EventCursor], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> MeasurementWatchState:
    """Watch until terminal state while preserving cursor state for reconnect."""

    if poll_interval_s < 0:
        raise ValueError("watch poll interval cannot be negative")
    while True:
        update = poll_measurement_watch(client, state)
        if on_summary is not None:
            on_summary(update.summary)
        if update.console_truncated and on_truncation is not None:
            on_truncation(update.event_page.oldest_retained_cursor)
        if update.event_page.events and on_events is not None:
            on_events(update.event_page)
        if state.is_terminal:
            return state
        sleep(poll_interval_s)


def load_client_config(path: str | Path = CMS_CLIENT_CONFIG) -> CMSClientConfig:
    """Load client connection settings from persistent JSON config."""

    config_path = Path(path)
    if not config_path.exists():
        return CMSClientConfig(base_url=f"http://{platform.node()}:8000", api_token="silabs-cms")

    with config_path.open("r", encoding="utf-8") as config_file:
        config = json.load(config_file)

    daemon = config.get("daemon", {})
    host = str(daemon.get("host", config.get("host", platform.node())))
    port = int(daemon.get("port", config.get("port", 8000)))
    scheme = str(daemon.get("scheme", config.get("scheme", "http")))
    base_url = str(daemon.get("base_url", config.get("base_url", ""))).strip()
    if not base_url:
        base_url = f"{scheme}://{host}:{port}"

    return CMSClientConfig(
        base_url=base_url,
        api_token=str(daemon.get("api_token", config.get("api_token", "silabs-cms"))),
        timeout_s=float(daemon.get("timeout_s", config.get("timeout_s", 30.0))),
        target={str(key): str(value) for key, value in config.get("target", {}).items()},
        build={str(key): str(value) for key, value in config.get("build", {}).items()},
        tools={str(key): str(value) for key, value in config.get("tools", {}).items()},
    )


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cms", description="CMS bench client")
    parser.add_argument("--config", default=CMS_CLIENT_CONFIG, help="client config JSON path")
    parser.add_argument("--base-url", default="", help="override daemon base URL")
    parser.add_argument("--api-token", default="", help="override daemon API token")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health", help="show daemon and bench health")
    subparsers.add_parser("status", help="show bench status")
    subparsers.add_parser("tests", aliases=["TESTS"], help="list test modes and features")

    build = subparsers.add_parser("build", aliases=["BUILD"], help="build local firmware with SLC config")
    build.add_argument("targets", nargs="*", help="optional .slcp targets or app directories")
    build.add_argument("--action", default="build", choices=["generate", "upgrade", "build", "rebuild"])
    build.add_argument("--sdk-package-path", default="", help="override unpacked SiSDK package path")
    build.add_argument("--sisdk-version", default="", help="override selected SiSDK version")
    build.add_argument("--build-root", default="", help="override firmware build root")
    build.add_argument("--bootloader-slcp", default="", help="override bootloader SLCP path")

    send_fw = subparsers.add_parser("send_fw", aliases=["SEND_FW"], help="send firmware and metadata to the bench")
    send_fw.add_argument("--request-json", help="load the full firmware request from JSON")
    send_fw.add_argument("--request-id", default="", help="firmware staging id")
    send_fw.add_argument("--dut-image", default="", help="DUT firmware path or URL")
    send_fw.add_argument("--tester-image", default="", help="tester firmware path or URL")
    send_fw.add_argument("--bootloader-image", default="", help="bootloader firmware path or URL")
    send_fw.add_argument("--dut-sha256", default="", help="expected DUT firmware SHA-256")
    send_fw.add_argument("--tester-sha256", default="", help="expected tester firmware SHA-256")
    send_fw.add_argument("--bootloader-sha256", default="", help="expected bootloader firmware SHA-256")
    send_fw.add_argument("--board", default="", help="target board id")
    send_fw.add_argument("--device", default="", help="target device id")
    send_fw.add_argument("--git-commit", default="", help="firmware source commit")
    send_fw.add_argument("--sisdk-version", default="", help="SiSDK version")
    send_fw.add_argument("--sisdk-commit", default="", help="SiSDK commit")
    send_fw.add_argument("--build-root", default="", help="override firmware build root used for provenance")
    send_fw.add_argument("--requested-by", default="", help="request owner")

    test = subparsers.add_parser("test", aliases=["TEST"], help="run a remote test with staged firmware")
    test.add_argument("mode", choices=[mode.value for mode in MeasurementMode])
    test.add_argument("feature")
    test.add_argument("--config-name", default="default", help="test config")
    test.add_argument(
        "--run-name",
        dest="run_name",
        default="",
        help="human-readable test run name",
    )
    test.add_argument("--out-dir", default="artifacts", help="local artifact root")
    test.add_argument(
        "--preview",
        action="store_true",
        help="show exactly what test would be submitted without starting it",
    )
    test.add_argument(
        "--verbose",
        action="store_true",
        help="show live stdout, stderr, and lifecycle events",
    )
    test.add_argument(
        "--poll-interval",
        type=_non_negative_float,
        default=1.0,
        help="seconds between observation polls",
    )
    test.add_argument(
        "--send-to-database",
        "--upload-results-to-database",
        action="store_true",
        dest="upload_results_to_database",
        help="also upload generated result JSON files to the configured database/data lake",
    )


    measurement = subparsers.add_parser("measurement", help="show measurement status")
    measurement.add_argument("measurement_id")

    watch = subparsers.add_parser(
        "watch",
        aliases=["WATCH"],
        help="attach to an active or explicit measurement",
    )
    watch.add_argument("measurement_id", nargs="?", default="")
    watch.add_argument(
        "--cursor",
        type=_non_negative_int,
        default=0,
        help="resume after this previously acknowledged event cursor",
    )
    watch.add_argument(
        "--verbose",
        action="store_true",
        help="show live stdout, stderr, and lifecycle events",
    )
    watch.add_argument(
        "--poll-interval",
        type=_non_negative_float,
        default=1.0,
        help="seconds between observation polls",
    )

    cancel = subparsers.add_parser("cancel", aliases=["CANCEL"], help="cancel the active test")

    artifacts = subparsers.add_parser(
        "artifacts",
        aliases=["ARTIFACTS"],
        help="download artifacts from an explicit measurement",
    )
    artifacts.add_argument("measurement_id")
    artifacts.add_argument("--out-dir", default="artifacts")
    return parser


def _measurement_exit_code(
    state: MeasurementState | None,
) -> CMSCLIExitCode:
    return {
        MeasurementState.PASSED: CMSCLIExitCode.SUCCESS,
        MeasurementState.FAILED: CMSCLIExitCode.TEST_FAILED,
        MeasurementState.CANCELLED: CMSCLIExitCode.TEST_CANCELLED,
        MeasurementState.REJECTED: CMSCLIExitCode.TEST_REJECTED,
    }.get(state, CMSCLIExitCode.CLIENT_ERROR)


def main(argv: list[str] | None = None) -> int:
    try:
        return int(_main(argv))
    except CMSConnectionError as exc:
        print(str(exc), file=sys.stderr)
        return int(CMSCLIExitCode.CONNECTION_FAILURE)
    except CMSProtocolMismatchError as exc:
        print(str(exc), file=sys.stderr)
        return int(CMSCLIExitCode.PROTOCOL_MISMATCH)
    except CMSDaemonError as exc:
        print(str(exc), file=sys.stderr)
        return int(CMSCLIExitCode.CLIENT_ERROR)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return int(CMSCLIExitCode.CLIENT_ERROR)


def _main(argv: list[str] | None = None) -> int:
    args = build_cli_parser().parse_args(argv)
    client_config = load_client_config(args.config)
    client = CMSClient(base_url=args.base_url, api_token=args.api_token, config_path=args.config)

    if args.command == "health":
        report = client.get_health()
        print(f"{report.state.value}: {report.summary}")
        for check in report.checks:
            detail = f" ({check.detail})" if check.detail else ""
            print(f"{check.status.value} {check.name}: {check.summary}{detail}")
        return (
            CMSCLIExitCode.SUCCESS
            if report.state is not BenchState.ERROR
            else CMSCLIExitCode.CLIENT_ERROR
        )

    if args.command == "status":
        status = client.get_status()
        print(f"{status.state.value}: {status.identity.bench_id}")
        if status.active_measurement_id:
            print(f"active_measurement_id: {status.active_measurement_id}")
        if status.detail:
            print(status.detail)
        return (
            CMSCLIExitCode.SUCCESS
            if status.state is not BenchState.ERROR
            else CMSCLIExitCode.CLIENT_ERROR
        )

    if args.command in {"tests", "TESTS"}:
        payload = client.list_tests()
        print("modes: " + ", ".join(payload.get("modes", [])))
        for feature in payload.get("features", []):
            configs = ", ".join(feature.get("configs", []))
            print(f"{feature.get('feature', '')}: {configs}")
        return CMSCLIExitCode.SUCCESS

    if args.command in {"build", "BUILD"}:
        return (
            CMSCLIExitCode.SUCCESS
            if _run_local_build(args, args.config) == 0
            else CMSCLIExitCode.CLIENT_ERROR
        )

    if args.command in {"send_fw", "SEND_FW"}:
        request = _request_from_send_fw_args(args, client_config)
        result = client.send_firmware(request)
        _print_measurement_result(result)
        if result.state is MeasurementState.REJECTED:
            return CMSCLIExitCode.TEST_REJECTED
        return (
            CMSCLIExitCode.CLIENT_ERROR
            if result.state is MeasurementState.FAILED
            else CMSCLIExitCode.SUCCESS
        )

    if args.command == "measurement":
        _print_measurement_result(client.get_measurement(args.measurement_id))
        return CMSCLIExitCode.SUCCESS

    if args.command in {"watch", "WATCH"}:
        measurement_id = _resolve_watch_measurement_id(client, args.measurement_id)
        state = _watch_remote_measurement(
            client,
            measurement_id,
            after_cursor=EventCursor(args.cursor),
            verbose=args.verbose,
            poll_interval_s=args.poll_interval,
        )
        return _measurement_exit_code(
            state.summary.state if state.summary is not None else None
        )

    if args.command in {"test", "TEST"}:
        if args.preview:
            _print_test_preview(args)
            return CMSCLIExitCode.SUCCESS
        result = client.run_test(
            MeasurementMode(args.mode),
            args.feature,
            config=args.config_name,
            run_name=args.run_name,
            upload_results_to_database=args.upload_results_to_database,
        )
        if not result.measurement_id:
            _print_measurement_result(result)
            return _measurement_exit_code(result.state)
        state = _watch_remote_measurement(
            client,
            result.measurement_id,
            verbose=args.verbose,
            poll_interval_s=args.poll_interval,
        )
        result = client.get_measurement(result.measurement_id)
        if result.artifacts:
            for path in client.download_measurement_artifacts(
                result.measurement_id,
                Path(args.out_dir),
            ):
                print(f"saved {path}")
        return _measurement_exit_code(
            state.summary.state if state.summary is not None else None
        )

    if args.command in {"cancel", "CANCEL"}:
        result = client.cancel_active_test()
        _print_measurement_result(result)
        if result.state is MeasurementState.REJECTED:
            return CMSCLIExitCode.TEST_REJECTED
        return (
            CMSCLIExitCode.CLIENT_ERROR
            if result.state is MeasurementState.FAILED
            else CMSCLIExitCode.SUCCESS
        )

    if args.command in {"artifacts", "ARTIFACTS"}:
        for path in client.download_measurement_artifacts(
            args.measurement_id,
            Path(args.out_dir),
        ):
            print(path)
        return CMSCLIExitCode.SUCCESS

    return CMSCLIExitCode.USAGE_ERROR


def _request_from_send_fw_args(
    args: argparse.Namespace,
    client_config: CMSClientConfig | None = None,
) -> MeasurementRequest:
    if args.request_json:
        with Path(args.request_json).open("r", encoding="utf-8") as request_file:
            return measurement_request_from_dict(json.load(request_file))

    target_cfg = client_config.target if client_config and client_config.target else {}
    build_cfg = client_config.build if client_config and client_config.build else {}
    build_root = Path(args.build_root or str(build_cfg.get("root", ".fw_build")))
    fw_data = _read_json_file(build_root / "fw_data.json")
    if not fw_data:
        raise RuntimeError(
            f"Build provenance is missing: {build_root / 'fw_data.json'}. "
            "Run `cms build` before creating a firmware request."
        )
    firmware_cfg = dict(fw_data.get("firmware", {}))
    firmware_cfg["build_root"] = str(build_root)
    sdk_cfg = fw_data.get("sdk", {})
    sdk_source = str(sdk_cfg.get("source", ""))
    sdk_version = str(sdk_cfg.get("version", firmware_cfg.get("sisdk", "")))
    sdk_commit = str(sdk_cfg.get("commit", ""))
    sdk_hash = str(sdk_cfg.get("hash", ""))
    recorded_xapi_hash = str(sdk_cfg.get("xapi_sha256", ""))
    missing_provenance = [
        name
        for name, value in (
            ("sdk.source", sdk_source),
            ("sdk.version", sdk_version),
            ("sdk.xapi_sha256", recorded_xapi_hash),
        )
        if not value
    ]
    if missing_provenance:
        raise RuntimeError(
            "Build provenance is incomplete in "
            f"{build_root / 'fw_data.json'}: {', '.join(missing_provenance)}. "
            "Run `cms build` again before creating a firmware request."
        )
    _require_matching_build_provenance("SiSDK version", args.sisdk_version, sdk_version)
    _require_matching_build_provenance("SiSDK commit", args.sisdk_commit, sdk_commit)
    dut_image_arg = args.dut_image or str(target_cfg.get("dut_image", ""))
    tester_image_arg = args.tester_image or str(target_cfg.get("tester_image", ""))
    bootloader_image_arg = args.bootloader_image or str(target_cfg.get("bootloader_image", ""))
    dut_image = _firmware_image_from_arg(dut_image_arg, "dut", firmware_cfg)
    tester_image = _firmware_image_from_arg(tester_image_arg, "tester", firmware_cfg)
    bootloader_image = _firmware_image_from_arg(
        bootloader_image_arg,
        "bootloader",
        firmware_cfg,
    ) if bootloader_image_arg or firmware_cfg.get("bootloader_app_hash") else None
    dut_sha256 = _selected_firmware_sha256(
        dut_image,
        requested=args.dut_sha256,
        fallback=str(target_cfg.get("dut_sha256", firmware_cfg.get("dut_app_hash", ""))),
    )
    tester_sha256 = _selected_firmware_sha256(
        tester_image,
        requested=args.tester_sha256,
        fallback=str(target_cfg.get("tester_sha256", firmware_cfg.get("tester_app_hash", ""))),
    )
    bootloader_sha256 = (
        _selected_firmware_sha256(
            bootloader_image,
            requested=args.bootloader_sha256,
            fallback=str(
                target_cfg.get("bootloader_sha256", firmware_cfg.get("bootloader_app_hash", ""))
            ),
        )
        if bootloader_image is not None
        else ""
    )
    xapi_file = Path(str(fw_data.get("build", {}).get("xapi", build_root / "runtime" / "sl_bt.xapi")))
    if not xapi_file.is_file():
        raise RuntimeError(f"Staged sl_bt.xapi from the selected build does not exist: {xapi_file}")
    xapi_hash = _sha256_file(xapi_file)
    if recorded_xapi_hash != xapi_hash:
        raise RuntimeError("Staged sl_bt.xapi hash does not match the selected build provenance")

    request_id = args.request_id or f"cms-{int(time.time())}"
    return MeasurementRequest(
        request_id=request_id,
        selector=SuiteSelector(mode="ble", feature="", config="default"),
        firmware=FirmwareData(
            dut_img=FirmwareImage(
                name=dut_image["name"],
                url=dut_image["url"],
                sha256=dut_sha256,
            ),
            tester_img=FirmwareImage(
                name=tester_image["name"],
                url=tester_image["url"],
                sha256=tester_sha256,
            ),
            bootloader_img=FirmwareImage(
                name=bootloader_image["name"],
                url=bootloader_image["url"],
                sha256=bootloader_sha256,
            ) if bootloader_image is not None else None,
            bootloader_img_hash=bootloader_sha256,
            board=args.board or str(target_cfg.get("board", firmware_cfg.get("board", ""))),
            device=args.device or str(target_cfg.get("device", firmware_cfg.get("device", ""))),
            xapi_file=FirmwareImage(
                name=xapi_file.name,
                url=str(xapi_file),
                sha256=xapi_hash,
            ),
            xapi_file_hash=xapi_hash,
            git_commit=args.git_commit,
            sisdk_source=sdk_source,
            sisdk_version=sdk_version,
            sisdk_commit=sdk_commit,
            sisdk_hash=sdk_hash,
        ),
        requested_by=args.requested_by,
    )


def _require_matching_build_provenance(label: str, requested: str, built: str) -> None:
    if requested and requested != built:
        raise RuntimeError(
            f"Requested {label} '{requested}' does not match the firmware build provenance '{built}'. "
            "Rebuild with the requested SDK selection before sending firmware."
        )


def _run_local_build(args: argparse.Namespace, config_path: str) -> int:
    cmd = [sys.executable, str(Path("apps") / "app_manager.py"), args.action, *args.targets]
    for option, value in (
        ("--config", config_path),
        ("--sdk-package-path", args.sdk_package_path),
        ("--sisdk-version", args.sisdk_version),
        ("--build-root", args.build_root),
        ("--bootloader-slcp", args.bootloader_slcp),
    ):
        if value:
            cmd.extend([option, value])
    result = subprocess.run(cmd, check=False)
    return result.returncode


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _discover_built_firmware(role: str, firmware_cfg: dict[str, Any]) -> Path:
    build_root = Path(str(firmware_cfg.get("build_root", ".fw_build")))
    candidates_by_role = {
        "dut": ["ncp_ble_dut_dcdc", "ncp_ble_dut"],
        "tester": ["ncp_ble_tester"],
        "bootloader": ["bootloader-uart-bgapi"],
    }
    for app_name in candidates_by_role[role]:
        app_dir = build_root / app_name
        matches = sorted(app_dir.rglob("*.s37")) if app_dir.exists() else []
        if matches:
            return matches[0]
    expected = ", ".join(candidates_by_role[role])
    raise RuntimeError(
        f"No built {role} firmware found under {build_root} for {expected}. "
        "Run `cms BUILD` first or pass an explicit firmware image path."
    )


def _firmware_image_from_arg(value: str, role: str, firmware_cfg: dict[str, Any]) -> dict[str, str]:
    if value.startswith("http://") or value.startswith("https://"):
        return {"name": value.rstrip("/").rsplit("/", 1)[-1], "url": value}
    path = Path(value) if value else _discover_built_firmware(role, firmware_cfg)
    return {"name": path.name, "url": str(path)}


def _selected_firmware_sha256(
    image: dict[str, str],
    *,
    requested: str,
    fallback: str,
) -> str:
    """Hash the exact local image that will be uploaded to the Bench."""

    if requested:
        return requested
    source = image["url"]
    if source.startswith("http://") or source.startswith("https://"):
        return fallback
    return _sha256_file(Path(source))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_local_test_config(feature: str, config: str) -> str:
    config_path = _local_test_config_path(feature, config)
    if not config_path.exists():
        raise RuntimeError(f"test config does not exist: {config_path}")
    try:
        config_data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"test config is not valid JSON: {config_path}") from exc
    if not isinstance(config_data, dict):
        raise RuntimeError(f"test config must be a JSON object: {config_path}")
    for section in ("firmware", "hw"):
        if section in config_data and not isinstance(config_data[section], dict):
            raise RuntimeError(f"test config section must be an object: {section}")
    return _sha256_file(config_path)


def _local_test_config_path(feature: str, config: str) -> Path:
    feature_name = feature.strip()
    if not feature_name:
        raise RuntimeError("test feature is required")
    if Path(feature_name).name != feature_name or feature_name in {".", ".."}:
        raise RuntimeError("test feature must be a single feature name")
    config_dir = Path("tests") / "configs" / "ble" / feature_name
    if not config_dir.is_dir():
        raise RuntimeError(f"test feature config directory does not exist: {config_dir}")
    config_name = (config or "default").strip().replace("\\", "/")
    if not config_name.endswith(".json"):
        config_name = f"{config_name}.json"
    relative = Path(config_name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise RuntimeError("test config must be a single relative config path")
    resolved = (config_dir / relative).resolve()
    try:
        resolved.relative_to(config_dir.resolve())
    except ValueError as exc:
        raise RuntimeError("test config must stay beneath the selected feature directory") from exc
    return resolved


def _local_firmware_payloads(request: MeasurementRequest) -> dict[str, str]:
    payloads: dict[str, str] = {}
    for key, image in (("dut_img", request.firmware.dut_img), ("tester_img", request.firmware.tester_img)):
        path = Path(image.url)
        if path.exists() and path.is_file():
            payloads[key] = base64.b64encode(path.read_bytes()).decode("ascii")
    if request.firmware.bootloader_img is not None:
        path = Path(request.firmware.bootloader_img.url)
        if path.exists() and path.is_file():
            payloads["bootloader_img"] = base64.b64encode(path.read_bytes()).decode("ascii")
    if request.firmware.xapi_file is not None:
        path = Path(request.firmware.xapi_file.url)
        if path.exists() and path.is_file():
            payloads["xapi_file"] = base64.b64encode(path.read_bytes()).decode("ascii")
    return payloads


def _print_measurement_result(result: MeasurementResult) -> None:
    print(f"{result.measurement_id}: {result.state.value} {result.progress_percent}%")
    if result.identity is not None:
        print(f"run_name: {result.identity.run_name}")
        print(f"created_at: {result.identity.created_at.isoformat()}")
    if result.message:
        print(result.message)
    for artifact in result.artifacts:
        location = artifact.url or str(artifact.path or "")
        print(f"artifact {artifact.name}: {location}")


def _print_test_preview(args: argparse.Namespace) -> None:
    config_path = _local_test_config_path(args.feature, args.config_name)
    config_hash = _validate_local_test_config(args.feature, args.config_name)
    print("test preview")
    print(f"mode: {args.mode}")
    print(f"feature: {args.feature}")
    print(f"config: {args.config_name}")
    print(f"config_path: {config_path}")
    print(f"config_hash: {config_hash}")
    if args.run_name:
        print(f"run_name: {args.run_name}")
    destination = "database" if args.upload_results_to_database else "cms_client"
    print(f"results_destination: {destination}")


def _resolve_watch_measurement_id(client: CMSClient, requested_measurement_id: str) -> str:
    measurement_id = requested_measurement_id.strip()
    if measurement_id:
        return measurement_id
    measurement_id = client.get_active_measurement_id()
    if measurement_id is None:
        raise RuntimeError("the CMS daemon has no active measurement")
    return measurement_id


def _measurement_summary_key(summary: MeasurementSummary) -> tuple[Any, ...]:
    return (
        summary.state,
        summary.counts,
        summary.current_test,
        summary.failed_tests,
    )


def _print_measurement_summary(summary: MeasurementSummary) -> None:
    counts = summary.counts
    print(f"{summary.measurement_id}: {summary.state.value}")
    print(
        "tests: "
        f"total={counts.total} completed={counts.completed} passed={counts.passed} "
        f"failed={counts.failed} skipped={counts.skipped} remaining={counts.remaining}"
    )
    if summary.current_test is not None:
        current = summary.current_test
        print(
            f"current: {current.ordinal}/{current.total_jobs} "
            f"{current.feature} {current.config}"
        )
    if summary.failed_tests:
        print("failed tests:")
        for failed in summary.failed_tests:
            print(
                f"  {failed.ordinal}/{failed.total_jobs} "
                f"{failed.feature} {failed.config}"
            )


def _print_verbose_event_page(page: MeasurementEventPage) -> None:
    for event in page.events:
        timestamp = event.timestamp.isoformat()
        if event.event_type == "console":
            source = str(event.details.get("source", "console"))
            text = str(event.details.get("text", ""))
            for line in text.splitlines() or [""]:
                print(f"{timestamp} [{source}] {line}")
            continue
        state = str(event.details.get("state", event.event_type))
        print(f"{timestamp} [event] {state}")


def _watch_remote_measurement(
    client: CMSClient,
    measurement_id: str,
    *,
    after_cursor: EventCursor = EventCursor(),
    verbose: bool = False,
    poll_interval_s: float = 1.0,
) -> MeasurementWatchState:
    state = MeasurementWatchState(measurement_id, cursor=after_cursor)
    last_summary_key: tuple[Any, ...] | None = None

    def render_summary(summary: MeasurementSummary) -> None:
        nonlocal last_summary_key
        summary_key = _measurement_summary_key(summary)
        if summary_key != last_summary_key:
            _print_measurement_summary(summary)
            last_summary_key = summary_key

    def render_truncation(oldest_retained_cursor: EventCursor) -> None:
        print(
            "warning: verbose console output was truncated; "
            f"continuing from cursor {oldest_retained_cursor.value}"
        )

    return watch_measurement(
        client,
        state,
        poll_interval_s=poll_interval_s,
        on_summary=render_summary,
        on_events=_print_verbose_event_page if verbose else None,
        on_truncation=render_truncation,
    )


if __name__ == "__main__":
    sys.exit(main())

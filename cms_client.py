"""Client-side helpers for talking to one CMS bench daemon.

`cms_client.py` is the remote control surface used by developers, CI, or other
automation. It speaks the daemon protocol and leaves bench-local hardware
control to `cmsd.py` and `cms_bench.py`.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable

from cms_colors import COLOR_CHOICES, ColorFormatter, color_formatter, state_role
from cms_diff_comparison import (
    ComparisonStatus,
    DifferentialPairingError,
    create_diff_comparison_report,
    pair_downloaded_differential_results,
    prepare_comparison_inputs,
    render_diff_comparison_table,
    write_diff_comparison_report,
)
from cms_protocol import (
    CMS_OBSERVATION_PROTOCOL_VERSION,
    Artifacts,
    BenchHealthReport,
    BenchIdentity,
    BenchState,
    BenchStatus,
    DetectedTargetIdentity,
    DifferentialFirmwareData,
    FirmwareImage,
    FirmwareData,
    FirmwareRoleImage,
    EventCursor,
    MeasurementEventPage,
    MeasurementMode,
    MeasurementRequest,
    MeasurementResult,
    MeasurementState,
    MeasurementSummary,
    SuiteSelector,
    VariableFilter,
    artifact_from_dict,
    artifact_to_dict,
    bench_health_report_from_dict,
    canonicalize_target_device,
    measurement_request_from_dict,
    measurement_request_to_dict,
    measurement_result_from_dict,
    measurement_event_page_from_dict,
    measurement_summary_from_dict,
)
from cms_variable_filters import (
    parse_variable_filters,
    validate_variable_filters,
    variable_help_text,
)

CMS_CLIENT_CONFIG = "cms_client_config.json"
TERMINAL_MEASUREMENT_STATES = frozenset(
    {
        MeasurementState.PASSED,
        MeasurementState.FAILED,
        MeasurementState.CANCELLED,
        MeasurementState.REJECTED,
        MeasurementState.INTERRUPTED,
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
    COMPARISON_DATA_FAILURE = 8
    THRESHOLD_REGRESSION = 9


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
class CMSBenchEndpointConfig:
    """One named CMS Bench daemon endpoint from client configuration."""

    scheme: str
    host: str
    port: int
    api_token: str
    timeout_s: float = 30.0

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class CMSResolvedEndpoint:
    """One concrete Client endpoint selected for the current invocation."""

    base_url: str
    api_token: str
    timeout_s: float
    source: str


@dataclass(slots=True)
class CMSClientConfig:
    """Configuration needed for one remote CMS daemon client."""

    base_url: str = ""
    api_token: str = ""
    timeout_s: float = 30.0
    target: dict[str, str] | None = None
    build: dict[str, str] | None = None
    tools: dict[str, str] | None = None
    diff: dict[str, str] | None = None
    benches: dict[str, CMSBenchEndpointConfig] | None = None


@dataclass(frozen=True, slots=True)
class TargetIdentityComparison:
    """Comparison evidence for one detected Bench target and requested target."""

    detected_board: str
    requested_board: str
    detected_device_raw: str
    detected_device_canonical: str
    requested_device: str
    requested_device_canonical: str
    mismatches: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not self.mismatches


def compare_detected_target_identity(
    detected: DetectedTargetIdentity,
    *,
    requested_board: str,
    requested_device: str,
) -> TargetIdentityComparison:
    """Compare detected Bench hardware with build/firmware target metadata."""

    detected_board = detected.radio_board_part_number.strip().upper()
    expected_board = requested_board.strip().upper()
    detected_device = detected.canonical_target_device or canonicalize_target_device(
        detected.target_device
    )
    expected_device = canonicalize_target_device(requested_device)
    mismatches: list[str] = []
    if not expected_board:
        mismatches.append("requested board is missing")
    elif detected_board != expected_board:
        mismatches.append(f"board detected={detected.radio_board_part_number} requested={requested_board}")
    if not expected_device:
        mismatches.append("requested device is missing")
    elif detected_device != expected_device:
        mismatches.append(
            "device "
            f"detected_raw={detected.target_device} "
            f"detected_canonical={detected_device} "
            f"requested={requested_device} "
            f"requested_canonical={expected_device}"
        )
    return TargetIdentityComparison(
        detected_board=detected.radio_board_part_number,
        requested_board=requested_board,
        detected_device_raw=detected.target_device,
        detected_device_canonical=detected_device,
        requested_device=requested_device,
        requested_device_canonical=expected_device,
        mismatches=tuple(mismatches),
    )


def refresh_and_record_bench_target(
    client: CMSClient,
    config_path: str | Path,
) -> DetectedTargetIdentity:
    """Fetch fresh Bench identity and record it in the sole Client config file."""

    report = client.get_health()
    if report.state is BenchState.ERROR:
        raise RuntimeError(f"CMS Bench health failed before workflow action: {report.summary}")
    detected = report.detected_identity
    if detected is None:
        raise RuntimeError("CMS Bench health did not provide a detected DUT identity")

    path = Path(config_path)
    if not path.is_file():
        raise RuntimeError(f"CMS client config does not exist: {path}")
    config = _read_json_file(path)
    target = config.get("target")
    if target is None:
        target = {}
        config["target"] = target
    if not isinstance(target, dict):
        raise RuntimeError("cms_client_config.json target must be an object")
    target.update(
        {
            "board": detected.radio_board_part_number.lower(),
            "board_rev": detected.radio_board_revision,
            "device": detected.target_device,
            "device_canonical": detected.canonical_target_device,
            "radio_board_serial": detected.radio_board_serial_number,
            "dut_jlink_serial": detected.dut_jlink_serial,
            "vcom_supported": detected.vcom_supported,
        }
    )
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return detected


def record_last_measurement(
    config_path: str | Path,
    result: MeasurementResult,
    *,
    mode: MeasurementMode,
    feature: str,
    config_name: str,
    run_name: str,
    variable_filters: tuple[VariableFilter, ...],
    endpoint: str,
) -> None:
    """Persist the locally recoverable identity of an accepted measurement."""

    if not result.measurement_id:
        return
    path = Path(config_path)
    if not path.is_file():
        return
    config = _read_json_file(path)
    config["last_measurement"] = {
        "measurement_id": result.measurement_id,
        "mode": mode.value,
        "feature": feature,
        "config": config_name,
        "run_name": run_name,
        "variable_filters": [
            {"name": item.name, "value": item.value}
            for item in variable_filters
        ],
        "endpoint": endpoint,
        "submitted_at": datetime.utcnow().isoformat(),
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def require_firmware_target_match(
    detected: DetectedTargetIdentity,
    firmware: FirmwareData,
) -> TargetIdentityComparison:
    """Reject firmware that does not match one fresh detected Bench target."""

    comparison = compare_detected_target_identity(
        detected,
        requested_board=firmware.board,
        requested_device=firmware.device,
    )
    if not comparison.matches:
        raise RuntimeError(
            "Firmware target does not match freshly detected CMS Bench target: "
            + "; ".join(comparison.mismatches)
        )
    return comparison


def require_fresh_firmware_target_match(
    client: CMSClient,
    config_path: str | Path,
    firmware: FirmwareData,
) -> TargetIdentityComparison:
    """Compatibility helper for callers that need to refresh before comparison."""

    return require_firmware_target_match(
        refresh_and_record_bench_target(client, config_path),
        firmware,
    )


def require_firmware_request_target_match(
    detected: DetectedTargetIdentity,
    request: MeasurementRequest,
) -> tuple[TargetIdentityComparison, ...]:
    """Validate normal firmware or each differential variant before staging."""

    variants: tuple[tuple[str, FirmwareData], ...]
    if request.differential_firmware is None:
        variants = (("firmware", request.firmware),)
    else:
        variants = (
            ("differential version1", request.differential_firmware.version1),
            ("differential version2", request.differential_firmware.version2),
        )
    comparisons: list[TargetIdentityComparison] = []
    mismatches: list[str] = []
    for variant_name, firmware in variants:
        comparison = compare_detected_target_identity(
            detected,
            requested_board=firmware.board,
            requested_device=firmware.device,
        )
        comparisons.append(comparison)
        mismatches.extend(f"{variant_name}: {item}" for item in comparison.mismatches)
    if mismatches:
        raise RuntimeError(
            "Firmware target does not match freshly detected CMS Bench target: "
            + "; ".join(mismatches)
        )
    return tuple(comparisons)


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
        timeout_s: float | None = None,
        config_path: str | Path = CMS_CLIENT_CONFIG,
    ):
        config = load_client_config(config_path)
        self.base_url = (base_url or config.base_url).rstrip("/")
        self.api_token = api_token or config.api_token
        self.timeout_s = config.timeout_s if timeout_s is None else timeout_s

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
        try:
            payload = self._request_json("POST", "/firmware", payload)
        except CMSConnectionError as exc:
            if str(exc) != "CMS daemon request timed out":
                raise
            reconciled = self._reconcile_timed_out_firmware_stage(request)
            if reconciled is None:
                raise
            return reconciled
        return measurement_result_from_dict(payload)

    def _reconcile_timed_out_firmware_stage(
        self,
        request: MeasurementRequest,
    ) -> MeasurementResult | None:
        """Return a completed stage only when its request ID exactly matches the timed-out POST."""

        try:
            result = self.get_measurement(request.request_id)
        except CMSConnectionError:
            return None
        if result.measurement_id != request.request_id:
            return None
        if result.state in {
            MeasurementState.ACCEPTED,
            MeasurementState.FAILED,
            MeasurementState.REJECTED,
        }:
            return result
        return None

    def get_staged(self) -> dict[str, Any]:
        """Return the daemon's current normal and differential firmware stages."""

        return self._request_json("GET", "/staged")

    def run_test(
        self,
        mode: MeasurementMode,
        feature: str,
        config: str = "default",
        run_name: str = "",
        variable_filters: tuple[VariableFilter, ...] = (),
        upload_results_to_database: bool = True,
        debug_artifact_streaming: bool = False,
    ) -> MeasurementResult:
        """Start a remote test using the most recently staged firmware."""

        config_hash = (
            ""
            if feature == "all" or _is_mutable_custom_config_name(config)
            else _validate_local_test_config(feature, config)
        )
        payload = self._request_json(
            "POST",
            "/test",
            {
                "mode": mode.value,
                "feature": feature,
                "config": config,
                "config_hash": config_hash,
                "variable_filters": [
                    {"name": item.name, "value": item.value}
                    for item in variable_filters
                ],
                "run_name": run_name,
                "upload_results_to_database": upload_results_to_database,
                "debug_artifact_streaming": debug_artifact_streaming,
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

    def list_measurement_artifacts(
        self,
        measurement_id: str,
        *,
        debug_persisted: bool = False,
    ) -> list[Artifacts]:
        """List RAM artifacts or the persisted debug bundle for one measurement."""

        encoded_id = urllib.parse.quote(measurement_id, safe="")
        payload = self._request_json(
            "GET",
            _observation_path(
                f"/measurement/{encoded_id}/artifacts",
                storage="debug" if debug_persisted else "runtime",
            ),
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

    def download_measurement_artifacts(
        self,
        measurement_id: str,
        out_dir: Path,
        *,
        debug_persisted: bool = False,
    ) -> list[Path]:
        """Download the RAM artifacts or persisted debug bundle for one measurement."""

        artifacts = self.list_measurement_artifacts(
            measurement_id,
            debug_persisted=debug_persisted,
        )
        if not artifacts:
            raise RuntimeError(f"measurement has no published artifacts: {measurement_id}")
        return [
            self._download_artifact_payload(artifact_to_dict(artifact), out_dir)
            for artifact in artifacts
        ]

    def failed_tests(self, measurement_id: str) -> list[dict[str, str]]:
        """Return failed test identities from structured orchestrator results."""

        artifacts = self.list_measurement_artifacts(measurement_id)
        results_artifact = next(
            (
                artifact
                for artifact in artifacts
                if Path(artifact.name.replace("\\", "/")).name == "orchestrator_results.jsonl"
            ),
            None,
        )
        if results_artifact is None:
            raise RuntimeError(
                f"measurement has no structured job-result artifact: {measurement_id}"
            )
        with tempfile.TemporaryDirectory() as temp_dir:
            results_path = self._download_artifact_payload(
                artifact_to_dict(results_artifact),
                Path(temp_dir),
            )
            records = [
                json.loads(line)
                for line in results_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        return _failed_test_records_from_job_results(records)

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

    benches: dict[str, CMSBenchEndpointConfig] = {}
    raw_benches = config.get("benches", {})
    if not isinstance(raw_benches, dict):
        raise ValueError("cms_client_config.json benches must be an object keyed by bench ID")
    for bench_id, raw_endpoint in raw_benches.items():
        if not isinstance(raw_endpoint, dict):
            raise ValueError(f"cms_client_config.json benches.{bench_id} must be an object")
        benches[str(bench_id)] = CMSBenchEndpointConfig(
            scheme=str(raw_endpoint.get("scheme", "http")),
            host=str(raw_endpoint.get("host", "")),
            port=int(raw_endpoint.get("port", 8000)),
            api_token=str(raw_endpoint.get("api_token", "")),
            timeout_s=float(raw_endpoint.get("timeout_s", daemon.get("timeout_s", 30.0))),
        )

    return CMSClientConfig(
        base_url=base_url,
        api_token=str(daemon.get("api_token", config.get("api_token", "silabs-cms"))),
        timeout_s=float(daemon.get("timeout_s", config.get("timeout_s", 30.0))),
        target={str(key): str(value) for key, value in config.get("target", {}).items()},
        build={str(key): str(value) for key, value in config.get("build", {}).items()},
        tools={str(key): str(value) for key, value in config.get("tools", {}).items()},
        diff={str(key): str(value) for key, value in config.get("diff", {}).items()},
        benches=benches,
    )


def resolve_client_endpoint(
    config: CMSClientConfig,
    *,
    base_url: str = "",
    api_token: str = "",
    bench_id: str = "",
    bench_ip: str = "",
) -> CMSResolvedEndpoint:
    """Resolve one endpoint without persisting a command-specific selection."""

    endpoint_selectors = [
        name
        for name, value in (("--base-url", base_url), ("--bench-id", bench_id), ("--bench-ip", bench_ip))
        if value
    ]
    if len(endpoint_selectors) > 1:
        raise ValueError("endpoint selectors cannot be combined: " + ", ".join(endpoint_selectors))

    if base_url:
        return CMSResolvedEndpoint(
            base_url=base_url.rstrip("/"),
            api_token=api_token or config.api_token,
            timeout_s=config.timeout_s,
            source="--base-url",
        )
    if bench_id:
        endpoint = (config.benches or {}).get(bench_id)
        if endpoint is None:
            raise ValueError(f"unknown CMS bench ID: {bench_id}")
        return CMSResolvedEndpoint(
            base_url=endpoint.base_url,
            api_token=api_token or endpoint.api_token,
            timeout_s=endpoint.timeout_s,
            source=f"bench ID {bench_id}",
        )
    if bench_ip:
        if "://" in bench_ip or "/" in bench_ip:
            raise ValueError("--bench-ip must be a host name or IP address, not a URL")
        parsed_default = urllib.parse.urlsplit(config.base_url)
        if not parsed_default.scheme or not parsed_default.hostname or parsed_default.port is None:
            raise ValueError("default daemon endpoint is invalid; cannot resolve --bench-ip")
        return CMSResolvedEndpoint(
            base_url=f"{parsed_default.scheme}://{bench_ip}:{parsed_default.port}",
            api_token=api_token or config.api_token,
            timeout_s=config.timeout_s,
            source="--bench-ip",
        )
    return CMSResolvedEndpoint(
        base_url=config.base_url,
        api_token=api_token or config.api_token,
        timeout_s=config.timeout_s,
        source="default daemon configuration",
    )


def _print_explicit_endpoint_selection(args: argparse.Namespace, client: CMSClient) -> None:
    """Show a one-shot endpoint choice without changing normal command output."""

    if args.base_url or args.bench_id or args.bench_ip:
        print(f"endpoint: {client.base_url}")


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


def _add_bench_endpoint_arguments(parser: argparse.ArgumentParser) -> None:
    """Add non-persistent bench selection options to one remote-workflow command."""

    parser.add_argument("--bench-id", default="", help="configured bench ID from the benches mapping")
    parser.add_argument("--bench-ip", default="", help="one-shot bench host name or IP address")


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cms",
        description="CMS bench client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Normal workflow:\n"
            "  1. cms health\n"
            "  2. cms status\n"
            "  3. cms tests\n"
            "  4. cms BUILD\n"
            "  5. cms SEND_FW\n"
            "  6. cms TEST --mode default --feature beacon --config-name default --preview\n"
            "  7. cms TEST --mode default --feature beacon --config-name default --out-dir artifacts\n"
            "  8. cms WATCH <measurement_id> --cursor <last_cursor>\n"
            "  9. cms CANCEL\n"
            " 10. cms ARTIFACTS <measurement_id> --out-dir artifacts\n\n"
            "Differential workflow:\n"
            "  cms HEALTH\n"
            "  cms BUILD --diff\n"
            "  cms SEND_FW --diff\n"
            "  cms TEST --mode diff --feature beacon --verbose\n\n"
            "  cms RECIPES prints copy/paste command recipes for the common workflows.\n\n"
            "  cms EXIT_CODES documents stable script outcomes.\n\n"
            "  cms TROUBLESHOOT prints operator diagnostics for common failures.\n\n"
            "Use cms <command> --help for command-specific examples and next steps."
        ),
    )
    parser.add_argument("--config", default=CMS_CLIENT_CONFIG, help="client config JSON path")
    parser.add_argument("--base-url", default="", help="override daemon base URL")
    parser.add_argument("--api-token", default="", help="override daemon API token")
    parser.add_argument(
        "--color",
        choices=COLOR_CHOICES,
        default="auto",
        help="color human output: auto, always, or never",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    health = subparsers.add_parser(
        "health",
        aliases=["HEALTH"],
        help="show daemon and bench health",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Endpoint selection:\n"
            "  With no endpoint option, use the default daemon configuration.\n"
            "  --bench-id selects one configured benches entry.\n"
            "  --bench-ip makes a one-shot host/IP override using default daemon settings.\n"
            "  --base-url makes a one-shot complete URL override.\n"
            "  Use only one endpoint selector; conflicting selectors are rejected.\n"
            "  --api-token may override the selected endpoint token.\n\n"
            "Examples:\n"
            "  cms HEALTH --bench-id cms-bench-001\n"
            "  cms HEALTH --bench-ip 192.0.2.10\n"
            "  cms --base-url http://cms-bench-001:8000 HEALTH"
        ),
    )
    _add_color_argument(health)
    _add_bench_endpoint_arguments(health)
    status = subparsers.add_parser("status", help="show bench status")
    _add_color_argument(status)
    tests = subparsers.add_parser("tests", aliases=["TESTS"], help="list test modes and features")
    _add_color_argument(tests)
    exit_codes = subparsers.add_parser(
        "exit_codes",
        aliases=["EXIT_CODES"],
        help="document stable CLI exit codes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  cms EXIT_CODES\n\n"
            "Use these codes to distinguish a completed test failure from a "
            "connection failure, protocol mismatch, rejected request, or client usage error."
        ),
    )
    _add_color_argument(exit_codes)
    troubleshoot = subparsers.add_parser(
        "troubleshoot",
        aliases=["TROUBLESHOOT"],
        help="print troubleshooting guidance for common CMS Client failures",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Topics:\n"
            "  health\n"
            "  hardware-mappings\n"
            "  firmware\n"
            "  configs\n"
            "  daemon\n"
            "  truncated-output\n"
            "  stalled-setup\n"
            "  artifacts\n\n"
            "Example:\n"
            "  cms TROUBLESHOOT --topic health"
        ),
    )
    _add_color_argument(troubleshoot)
    troubleshoot.add_argument(
        "--topic",
        default="all",
        choices=[
            "all",
            "health",
            "hardware-mappings",
            "firmware",
            "configs",
            "daemon",
            "truncated-output",
            "stalled-setup",
            "artifacts",
        ],
        help="show one troubleshooting topic instead of all topics",
    )

    build = subparsers.add_parser(
        "build",
        aliases=["BUILD"],
        help="build local firmware with SLC config",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  cms BUILD\n\n"
            "Next:\n"
            "  Run cms SEND_FW to stage the built firmware on the bench."
        ),
    )
    _add_color_argument(build)
    build.add_argument("targets", nargs="*", help="optional .slcp targets or app directories")
    build.add_argument("--action", default="build", choices=["generate", "upgrade", "build", "rebuild"])
    build.add_argument("--sdk-package-path", default="", help="override unpacked SiSDK package path")
    build.add_argument("--build-root", default="", help="override firmware build root")
    build.add_argument("--bootloader-slcp", default="", help="override bootloader SLCP path")
    build.add_argument(
        "--diff",
        action="store_true",
        help=(
            "build configured version1 and version2 SiSDK packages in isolated diff roots"
        ),
    )
    _add_bench_endpoint_arguments(build)

    send_fw = subparsers.add_parser(
        "send_fw",
        aliases=["SEND_FW"],
        help="send firmware and metadata to the bench",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  cms SEND_FW\n"
            "  cms SEND_FW --diff\n\n"
            "Defaults:\n"
            "  Firmware images and sl_bt.xapi are discovered from the configured build root.\n\n"
            "Next:\n"
            "  Run cms TEST --mode default --feature beacon --config-name default --preview before starting a test.\n"
            "  After cms SEND_FW --diff, run cms TEST --mode diff --feature beacon --verbose."
        ),
    )
    _add_color_argument(send_fw)
    _add_bench_endpoint_arguments(send_fw)
    send_fw.add_argument("--request-json", help="load the full firmware request from JSON")
    send_fw.add_argument(
        "--diff",
        action="store_true",
        help="stage the version1/version2 firmware pair from diff_fw_data.json",
    )
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

    staged = subparsers.add_parser(
        "staged",
        aliases=["STAGED"],
        help="show the current normal and differential firmware stages",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Read-only view:\n"
            "  cms STAGED\n\n"
            "Shows the one current normal stage and the one current differential stage, if present."
        ),
    )
    _add_color_argument(staged)
    _add_bench_endpoint_arguments(staged)

    test = subparsers.add_parser(
        "test",
        aliases=["TEST"],
        help="run a remote test with staged firmware",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  cms TEST --mode default --feature beacon --config-name default --preview\n"
            "  cms TEST -m default -f beacon --config-name default --out-dir artifacts\n"
            "  cms TEST --mode default --feature beacon --config-name dcdc --preview\n"
            "  cms TEST --mode full_sweep --feature beacon --variable phy=2m --preview\n"
            "  cms TEST -m full_sweep -f beacon --variable phy=2m --variable dcdc=On --out-dir artifacts\n\n"
            "  cms TEST --mode diff --feature beacon --verbose\n\n"
            "Required options:\n"
            "  --mode, -m      default, full_sweep, or diff\n"
            "  --feature, -f  BLE feature name such as beacon, or all\n\n"
            "Database policy:\n"
            "  all runs upload results to the configured database.\n\n"
            "Variable filters:\n"
            "  Use --variable name=value to target generated configs by parameter.\n"
            "  Names and values come from the selected feature's parameters.json.\n"
            "  For beacon today, parameters.json declares phy values coded, 1m, 2m.\n"
            "  Run --preview to print accepted variable names and values before testing.\n\n"
            "Defaults:\n"
            "  --config-name default\n"
            "  --out-dir artifacts\n"
            "  --poll-interval 1.0\n\n"
            "Next:\n"
            "  The command watches automatically. Reattach later with cms WATCH <measurement_id>."
        ),
    )
    _add_color_argument(test)
    _add_bench_endpoint_arguments(test)
    test.add_argument(
        "--mode",
        "-m",
        required=True,
        choices=[
            MeasurementMode.DEFAULT.value,
            MeasurementMode.FULL_SWEEP.value,
            MeasurementMode.DIFF.value,
        ],
    )
    test.add_argument(
        "--feature",
        "-f",
        required=True,
        help="BLE feature name, or all to select every available feature",
    )
    test.add_argument("--config-name", default="default", help="test config")
    test.add_argument(
        "--variable",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=(
            "filter generated configs by a parameter declared in parameters.json; "
            "repeat to intersect filters"
        ),
    )
    test.add_argument(
        "--run-name",
        dest="run_name",
        default="",
        help="human-readable test run name",
    )
    test.add_argument(
        "--upload-results-to-database",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="upload completed results to the configured database (default: enabled)",
    )
    test.add_argument("--out-dir", default="artifacts", help="local artifact root")
    test.add_argument(
        "--debug",
        action="store_true",
        help="retrieve completed test result artifacts while the measurement is running",
    )
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
    custom = subparsers.add_parser(
        "custom",
        aliases=["CUSTOM"],
        help="update a feature's mutable custom.json configuration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  cms CUSTOM --feature beacon --set advertiser.interval=100\n"
            "  cms CUSTOM --feature beacon --set advertiser.phy=2m --set hw.supply_v=3.3\n\n"
            "Values are parsed as JSON when possible; unquoted text is stored as a string.\n"
            "Only tests/configs/ble/<feature>/custom.json is modified."
        ),
    )
    _add_color_argument(custom)
    custom.add_argument("--feature", "-f", required=True, help="BLE feature whose custom.json is updated")
    custom.add_argument(
        "--set",
        action="append",
        required=True,
        metavar="PATH=VALUE",
        help="replace an existing custom.json value using a dotted object path; repeat as needed",
    )
    measurement = subparsers.add_parser(
        "measurement",
        help="show measurement status",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  cms measurement <measurement_id>\n\n"
            "Next:\n"
            "  Use cms WATCH <measurement_id> to follow a running measurement."
        ),
    )
    _add_color_argument(measurement)
    measurement.add_argument("measurement_id")

    watch = subparsers.add_parser(
        "watch",
        aliases=["WATCH"],
        help="attach to an active or explicit measurement",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  cms WATCH\n"
            "  cms WATCH --reconnect\n"
            "  cms WATCH <measurement_id> --cursor 0\n"
            "  cms WATCH <measurement_id> --cursor <last_cursor> --verbose\n\n"
            "Defaults:\n"
            "  measurement_id omitted means attach to the active measurement.\n"
            "  --reconnect resumes the last submitted measurement recorded locally.\n"
            "  --cursor 0 starts from the beginning of retained events.\n"
            "  --poll-interval 1.0\n\n"
            "Next:\n"
            "  After completion, run cms ARTIFACTS <measurement_id> --out-dir artifacts."
        ),
    )
    _add_color_argument(watch)
    watch.add_argument("measurement_id", nargs="?", default="")
    watch.add_argument(
        "--reconnect",
        action="store_true",
        help="resume the last submitted measurement recorded in cms_client_config.json",
    )
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

    cancel = subparsers.add_parser(
        "cancel",
        aliases=["CANCEL"],
        help="cancel the active test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  cms CANCEL\n\n"
            "Scope:\n"
            "  Requests cancellation of the active measurement on the bench.\n\n"
            "Next:\n"
            "  Run cms WATCH to observe the final cancelled state."
        ),
    )
    _add_color_argument(cancel)

    artifacts = subparsers.add_parser(
        "artifacts",
        aliases=["ARTIFACTS"],
        help="download artifacts from an explicit measurement",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  cms ARTIFACTS <measurement_id> --out-dir artifacts\n"
            "  cms ARTIFACTS --debug --out-dir artifacts  # saved debug bundle\n\n"
            "Optional argument:\n"
            "  measurement_id   explicit measurement whose artifacts should be downloaded\n\n"
            "Defaults:\n"
            "  --out-dir artifacts\n\n"
            "Next:\n"
            "  Inspect the downloaded result JSON, JUnit XML, logs, and measurement artifacts."
        ),
    )
    _add_color_argument(artifacts)
    artifacts.add_argument(
        "measurement_id",
        nargs="?",
        default="",
        help="measurement id; omit to use the saved ongoing debug measurement",
    )
    artifacts.add_argument("--out-dir", default="artifacts")
    artifacts.add_argument(
        "--debug",
        action="store_true",
        help="download the saved debug measurement's persistent artifact bundle",
    )

    failed = subparsers.add_parser(
        "failed",
        aliases=["FAILED"],
        help="show failed test identities and rerun commands",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  cms FAILED <measurement_id>\n"
            "  cms FAILED <measurement_id> --json\n\n"
            "Source:\n"
            "  Uses structured orchestrator_results.jsonl records when available.\n\n"
            "Next:\n"
            "  Use the printed rerun command for each failed config."
        ),
    )
    _add_color_argument(failed)
    failed.add_argument("measurement_id")
    failed.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit machine-readable failed-test records",
    )

    recipes = subparsers.add_parser(
        "recipes",
        aliases=["RECIPES"],
        help="print copy/paste recipes for common CMS workflows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Focused workflows:\n"
            "  default config\n"
            "  exact named config\n"
            "  attach or reconnect from a cursor\n"
            "  cancel the active measurement\n"
            "  download artifacts by explicit measurement ID\n\n"
            "Example:\n"
            "  cms RECIPES --feature beacon --config-name dcdc --measurement-id <id>"
        ),
    )
    _add_color_argument(recipes)
    recipes.add_argument("--feature", default="beacon", help="feature used in TEST examples")
    recipes.add_argument("--config-name", default="dcdc", help="named config used in exact-config examples")
    recipes.add_argument("--measurement-id", default="<measurement_id>", help="measurement ID used in WATCH and ARTIFACTS examples")
    recipes.add_argument("--cursor", type=_non_negative_int, default=0, help="cursor used in reconnect examples")
    recipes.add_argument("--out-dir", default="artifacts", help="artifact output directory used in examples")
    return parser


def _add_color_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--color",
        choices=COLOR_CHOICES,
        default=argparse.SUPPRESS,
        help="color human output: auto, always, or never",
    )


def _measurement_exit_code(
    state: MeasurementState | None,
) -> CMSCLIExitCode:
    return {
        MeasurementState.PASSED: CMSCLIExitCode.SUCCESS,
        MeasurementState.FAILED: CMSCLIExitCode.TEST_FAILED,
        MeasurementState.CANCELLED: CMSCLIExitCode.TEST_CANCELLED,
        MeasurementState.REJECTED: CMSCLIExitCode.TEST_REJECTED,
        MeasurementState.INTERRUPTED: CMSCLIExitCode.CLIENT_ERROR,
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
    colors = color_formatter(args.color)
    client_config = load_client_config(args.config)
    uses_resolved_endpoint = (
        args.command == "health"
        or args.command in {"send_fw", "SEND_FW", "test", "TEST"}
        or args.command in {"build", "BUILD"}
    )
    if uses_resolved_endpoint:
        endpoint = resolve_client_endpoint(
            client_config,
            base_url=args.base_url,
            api_token=args.api_token,
            bench_id=args.bench_id,
            bench_ip=args.bench_ip,
        )
        client = CMSClient(
            base_url=endpoint.base_url,
            api_token=endpoint.api_token,
            timeout_s=endpoint.timeout_s,
            config_path=args.config,
        )
    else:
        client = CMSClient(base_url=args.base_url, api_token=args.api_token, config_path=args.config)

    if args.command == "health":
        report = client.get_health()
        print(f"{colors.state(report.state)}: {report.summary}")
        _print_explicit_endpoint_selection(args, client)
        print(f"bench_id: {report.identity.bench_id}")
        if report.detected_identity is None:
            print("detected_dut_identity: unavailable")
        else:
            detected = report.detected_identity
            revision = f" Rev. {detected.radio_board_revision}" if detected.radio_board_revision else ""
            vcom = "unknown" if detected.vcom_supported is None else str(detected.vcom_supported).lower()
            print(
                "detected_radio_board: "
                f"{detected.radio_board_part_number}{revision} "
                f"(serial {detected.radio_board_serial_number})"
            )
            print(
                "detected_target_device: "
                f"{detected.target_device} "
                f"(canonical {detected.canonical_target_device})"
            )
            print(f"detected_dut_jlink_serial: {detected.dut_jlink_serial}")
            print(f"detected_vcom_supported: {vcom}")
        for check in report.checks:
            detail = f" ({check.detail})" if check.detail else ""
            print(f"{colors.state(check.status.value)} {check.name}: {check.summary}{detail}")
        return (
            CMSCLIExitCode.SUCCESS
            if report.state is not BenchState.ERROR
            else CMSCLIExitCode.CLIENT_ERROR
        )

    if args.command == "status":
        status = client.get_status()
        print(f"{colors.state(status.state)}: {status.identity.bench_id}")
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
        modes = [mode for mode in payload.get("modes", []) if mode != MeasurementMode.DEBUG.value]
        print("modes: " + ", ".join(modes))
        for feature in payload.get("features", []):
            configs = ", ".join(feature.get("configs", []))
            print(f"{feature.get('feature', '')}: {configs}")
        return CMSCLIExitCode.SUCCESS

    if args.command in {"exit_codes", "EXIT_CODES"}:
        _print_exit_codes()
        return CMSCLIExitCode.SUCCESS

    if args.command in {"troubleshoot", "TROUBLESHOOT"}:
        _print_troubleshooting(args.topic)
        return CMSCLIExitCode.SUCCESS

    if args.command in {"build", "BUILD"}:
        _print_explicit_endpoint_selection(args, client)
        detected = refresh_and_record_bench_target(client, args.config)
        print(
            "recorded_bench_target: "
            f"{detected.radio_board_part_number} {detected.target_device}"
        )
        return (
            CMSCLIExitCode.SUCCESS
            if _run_local_build(args, args.config) == 0
            else CMSCLIExitCode.CLIENT_ERROR
        )

    if args.command in {"send_fw", "SEND_FW"}:
        _print_explicit_endpoint_selection(args, client)
        detected = refresh_and_record_bench_target(client, args.config)
        client_config = load_client_config(args.config)
        request = _request_from_send_fw_args(args, client_config)
        require_firmware_request_target_match(detected, request)
        result = client.send_firmware(request)
        _print_measurement_result(result, colors=colors)
        if result.state is MeasurementState.REJECTED:
            return CMSCLIExitCode.TEST_REJECTED
        return (
            CMSCLIExitCode.CLIENT_ERROR
            if result.state is MeasurementState.FAILED
            else CMSCLIExitCode.SUCCESS
        )

    if args.command in {"staged", "STAGED"}:
        _print_explicit_endpoint_selection(args, client)
        _print_staged_firmware(client.get_staged())
        return CMSCLIExitCode.SUCCESS

    if args.command == "measurement":
        _print_measurement_result(client.get_measurement(args.measurement_id), colors=colors)
        return CMSCLIExitCode.SUCCESS

    if args.command in {"watch", "WATCH"}:
        measurement_id = _resolve_watch_measurement_id(
            client,
            args.measurement_id,
            reconnect=args.reconnect,
            config_path=args.config,
        )
        state = _watch_remote_measurement(
            client,
            measurement_id,
            after_cursor=EventCursor(args.cursor),
            verbose=args.verbose,
            poll_interval_s=args.poll_interval,
            colors=colors,
        )
        return _measurement_exit_code(
            state.summary.state if state.summary is not None else None
        )

    if args.command in {"test", "TEST"}:
        _print_explicit_endpoint_selection(args, client)
        variable_filters = parse_variable_filters(args.variable)
        if args.feature == "all" and args.config_name != "default":
            raise RuntimeError("--feature all only supports the default config")
        if args.feature == "all" and variable_filters:
            raise RuntimeError("--feature all cannot be combined with --variable")
        if variable_filters:
            validate_variable_filters(_local_parameters_path(args.feature), variable_filters)
        if args.preview:
            _print_test_preview(args, variable_filters)
            return CMSCLIExitCode.SUCCESS
        return _run_test_feature(client, args, variable_filters, colors)

    if args.command in {"custom", "CUSTOM"}:
        path = update_custom_test_config(args.feature, args.set)
        print(path)
        return CMSCLIExitCode.SUCCESS

    if args.command in {"cancel", "CANCEL"}:
        result = client.cancel_active_test()
        _print_measurement_result(result, colors=colors)
        if result.state is MeasurementState.REJECTED:
            return CMSCLIExitCode.TEST_REJECTED
        return (
            CMSCLIExitCode.CLIENT_ERROR
            if result.state is MeasurementState.FAILED
            else CMSCLIExitCode.SUCCESS
        )

    if args.command in {"artifacts", "ARTIFACTS"}:
        measurement_id = (
            args.measurement_id
            or _saved_debug_measurement_id(args.config)
        )
        for path in client.download_measurement_artifacts(
            measurement_id,
            Path(args.out_dir),
            debug_persisted=args.debug,
        ):
            print(path)
        return CMSCLIExitCode.SUCCESS

    if args.command in {"failed", "FAILED"}:
        failed_tests = client.failed_tests(args.measurement_id)
        _print_failed_tests(args.measurement_id, failed_tests, as_json=args.as_json)
        return CMSCLIExitCode.SUCCESS

    if args.command in {"recipes", "RECIPES"}:
        _print_operator_recipes(args)
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
    if getattr(args, "diff", False):
        return _request_from_diff_build_manifest(args, build_root)
    fw_data = _read_json_file(build_root / "fw_data.json")
    if not fw_data:
        raise RuntimeError(
            f"Build provenance is missing: {build_root / 'fw_data.json'}. "
            "Run `cms build` before creating a firmware request."
        )
    firmware_cfg = dict(fw_data.get("firmware", {}))
    firmware_cfg["build_root"] = str(build_root)
    vcom_metadata = _vcom_metadata_from_fw_data(fw_data, build_root)
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
    dut_image = _firmware_image_from_arg(dut_image_arg, "dut_dcdc", firmware_cfg)
    no_dcdc_dut_image = _firmware_image_from_arg("", "dut_no_dcdc", firmware_cfg)
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
    no_dcdc_dut_sha256 = _selected_firmware_sha256(
        no_dcdc_dut_image,
        requested="",
        fallback="",
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
    sdk_package_path = Path(str(sdk_cfg.get("package_path", "")))
    if not sdk_package_path.is_dir():
        raise RuntimeError(
            "Selected firmware build does not record a usable SiSDK package path: "
            f"{sdk_package_path}"
        )
    sisdk_name = _sisdk_name_from_properties(sdk_package_path)
    if not sisdk_name:
        raise RuntimeError(
            "Selected SiSDK package does not declare an SDK id in .properties: "
            f"{sdk_package_path}"
        )
    xapi_file = sdk_package_path / "bluetooth_le_host" / "api" / "sl_bt.xapi"
    if not xapi_file.is_file():
        raise RuntimeError(
            "Selected SiSDK BGAPI definition does not exist: "
            f"{xapi_file}"
        )
    xapi_hash = _sha256_file(xapi_file)
    if recorded_xapi_hash != xapi_hash:
        raise RuntimeError(
            "Selected SiSDK BGAPI definition hash does not match the firmware build provenance"
        )

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
            dut_variants={
                "dcdc": FirmwareImage(
                    name=dut_image["name"],
                    url=dut_image["url"],
                    sha256=dut_sha256,
                ),
                "no_dcdc": FirmwareImage(
                    name=no_dcdc_dut_image["name"],
                    url=no_dcdc_dut_image["url"],
                    sha256=no_dcdc_dut_sha256,
                ),
            },
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
            sisdk_name=sisdk_name,
            sisdk_source=sdk_source,
            sisdk_version=sdk_version,
            sisdk_commit=sdk_commit,
            sisdk_hash=sdk_hash,
            vcom=vcom_metadata,
        ),
        requested_by=args.requested_by,
    )


def _request_from_diff_build_manifest(args: argparse.Namespace, build_root: Path) -> MeasurementRequest:
    """Create one paired staging request from the app manager's diff build manifest."""

    diff_manifest = _read_json_file(build_root / "diff_fw_data.json")
    variants = diff_manifest.get("variants")
    if not isinstance(variants, dict):
        raise RuntimeError(
            f"Differential build provenance is missing: {build_root / 'diff_fw_data.json'}. "
            "Run `cms build --diff` before staging differential firmware."
        )

    def variant_firmware(name: str) -> FirmwareData:
        variant = variants.get(name)
        if not isinstance(variant, dict) or not isinstance(variant.get("manifest"), dict):
            raise RuntimeError(f"Differential build manifest is missing the {name} variant")
        manifest = variant["manifest"]
        firmware = manifest.get("firmware")
        build = manifest.get("build")
        sdk = manifest.get("sdk")
        if not isinstance(firmware, dict) or not isinstance(build, dict) or not isinstance(sdk, dict):
            raise RuntimeError(f"Differential {name} build manifest is incomplete")
        artifacts = firmware.get("artifacts")
        if not isinstance(artifacts, list):
            raise RuntimeError(f"Differential {name} build manifest has no firmware artifacts")
        all_role_images = tuple(
            FirmwareRoleImage(
                role=str(item.get("role", "")),
                project_name=str(item.get("project_name", "")),
                image=FirmwareImage(
                    name=Path(str(item.get("image", ""))).name,
                    url=str(item.get("image", "")),
                    sha256=str(item.get("sha256", "")),
                ),
            )
            for item in artifacts
            if isinstance(item, dict)
        )
        by_role = {item.role: item.image for item in all_role_images}
        required_roles = {"dut_dcdc", "dut_no_dcdc", "tester"}
        missing_roles = sorted(required_roles - set(by_role))
        if missing_roles:
            raise RuntimeError(f"Differential {name} build is missing roles: {', '.join(missing_roles)}")
        unsupported_roles = sorted(
            f"{item.role}={item.image.name}"
            for item in all_role_images
            if item.image.name and Path(item.image.name).suffix.lower() not in {".s37", ".hex"}
        )
        if unsupported_roles:
            raise RuntimeError(
                f"Differential {name} build contains non-flashable firmware artifact(s): "
                f"{', '.join(unsupported_roles)}. Run `cms BUILD --diff` to produce .s37 or .hex images."
            )
        xapi_path = str(build.get("xapi", ""))
        xapi_hash = str(sdk.get("xapi_sha256", ""))
        return FirmwareData(
            dut_img=by_role["dut_dcdc"],
            tester_img=by_role["tester"],
            dut_variants={"dcdc": by_role["dut_dcdc"], "no_dcdc": by_role["dut_no_dcdc"]},
            bootloader_img=by_role.get("bootloader"),
            board=str(firmware.get("board", "")),
            device=str(firmware.get("device", "")),
            xapi_file=FirmwareImage(name=Path(xapi_path).name, url=xapi_path, sha256=xapi_hash),
            xapi_file_hash=xapi_hash,
            sisdk_source=str(sdk.get("source", "")),
            sisdk_version=str(sdk.get("version", "")),
            sisdk_commit=str(sdk.get("commit", "")),
            sisdk_hash=str(sdk.get("hash", "")),
            role_images=tuple(item for item in all_role_images if item.role != "bootloader"),
        )

    version1 = variant_firmware("version1")
    version2 = variant_firmware("version2")
    request_id = args.request_id or f"cms-diff-{int(time.time())}"
    return MeasurementRequest(
        request_id=request_id,
        selector=SuiteSelector(mode="ble", feature="", config="default"),
        firmware=version2,
        mode=MeasurementMode.DIFF,
        requested_by=args.requested_by,
        differential_firmware=DifferentialFirmwareData(version1=version1, version2=version2),
    )


def _require_matching_build_provenance(label: str, requested: str, built: str) -> None:
    if requested and requested != built:
        raise RuntimeError(
            f"Requested {label} '{requested}' does not match the firmware build provenance '{built}'. "
            "Rebuild with the requested SDK selection before sending firmware."
        )


def _vcom_metadata_from_fw_data(fw_data: dict[str, Any], build_root: Path) -> dict[str, Any]:
    vcom_metadata = fw_data.get("vcom", {})
    if vcom_metadata is None:
        return {}
    if not isinstance(vcom_metadata, dict):
        raise RuntimeError(
            f"Build provenance has invalid VCOM metadata in {build_root / 'fw_data.json'}: "
            "field 'vcom' must be an object."
        )
    return dict(vcom_metadata)


def _sisdk_name_from_properties(sdk_package_path: Path) -> str:
    properties_path = sdk_package_path / ".properties"
    if not properties_path.is_file():
        return ""
    for line in properties_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "id" and value.strip():
            return value.strip()
    return ""


def _run_local_build(args: argparse.Namespace, config_path: str) -> int:
    is_diff = bool(getattr(args, "diff", False))
    if not is_diff:
        _sync_config_sisdk_version(config_path, args.sdk_package_path)
    cmd = [sys.executable, str(Path("apps") / "app_manager.py"), args.action, *args.targets]
    if is_diff:
        cmd.append("--diff")
    for option, value in (
        ("--config", config_path),
        ("--sdk-package-path", args.sdk_package_path),
        ("--build-root", args.build_root),
        ("--bootloader-slcp", args.bootloader_slcp),
    ):
        if value:
            cmd.extend([option, value])
    result = subprocess.run(cmd, check=False)
    return result.returncode


def _sync_config_sisdk_version(config_path: str | Path, sdk_package_path: str = "") -> str:
    """Record the selected SDK's installed version in persistent client config."""

    path = Path(config_path)
    if not path.is_file():
        raise RuntimeError(f"CMS client config does not exist: {path}")
    config = _read_json_file(path)
    target = config.get("target")
    if not isinstance(target, dict):
        raise RuntimeError("cms_client_config.json must contain a target object")
    selected_path = sdk_package_path or str(target.get("sisdk_package_path", ""))
    if not selected_path:
        raise RuntimeError(
            "No SiSDK package path is configured. Set target.sisdk_package_path in "
            "cms_client_config.json or pass --sdk-package-path <path> to cms BUILD."
        )
    sdk_path = Path(selected_path).expanduser()
    if not sdk_path.is_absolute():
        sdk_path = (path.parent / sdk_path).resolve()
    properties_path = sdk_path / ".properties"
    if not properties_path.is_file():
        raise RuntimeError(f"SiSDK package path is missing .properties: {properties_path}")
    version = ""
    for line in properties_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "version" and value.strip():
            version = value.strip()
            break
    if not version:
        raise RuntimeError(f"SiSDK package properties are missing version: {properties_path}")
    target["sisdk"] = version
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return version


def _record_debug_measurement(
    args: argparse.Namespace,
    measurement_id: str,
    variable_filters: tuple[VariableFilter, ...],
) -> None:
    """Record an ongoing debug measurement for later artifact retrieval."""

    path = Path(args.config)
    config = _read_json_file(path)
    record = _measurement_record(args, measurement_id, variable_filters)
    record["status"] = "ongoing"
    config["debug_measurement"] = record
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _record_last_measurement(
    args: argparse.Namespace,
    measurement_id: str,
    variable_filters: tuple[VariableFilter, ...],
) -> None:
    """Record the most recently accepted test measurement in client config."""

    path = Path(args.config)
    config = _read_json_file(path)
    config["last_measurement"] = _measurement_record(args, measurement_id, variable_filters)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _measurement_record(
    args: argparse.Namespace,
    measurement_id: str,
    variable_filters: tuple[VariableFilter, ...],
) -> dict[str, Any]:
    """Build one persistent client-side measurement reference."""

    path = Path(args.config)
    config = _read_json_file(path)
    endpoint = args.base_url.strip()
    if not endpoint:
        daemon = config.get("daemon", {})
        if isinstance(daemon, dict):
            base_url = str(daemon.get("base_url", "")).strip()
            if base_url:
                endpoint = base_url.rstrip("/")
            else:
                scheme = str(daemon.get("scheme", config.get("scheme", "http")))
                host = str(daemon.get("host", config.get("host", platform.node())))
                port = int(daemon.get("port", config.get("port", 8000)))
                endpoint = f"{scheme}://{host}:{port}"
        else:
            endpoint = f"http://{platform.node()}:8000"

    return {
        "measurement_id": measurement_id,
        "mode": args.mode,
        "feature": args.feature,
        "config": args.config_name,
        "run_name": args.run_name,
        "variable_filters": [
            {"name": item.name, "value": item.value}
            for item in variable_filters
        ],
        "endpoint": endpoint,
        "out_dir": args.out_dir,
        "submitted_at": datetime.utcnow().isoformat(),
    }


def _saved_debug_measurement_id(config_path: str | Path) -> str:
    """Return the measurement recorded by the most recent ongoing debug run."""

    config = _read_json_file(Path(config_path))
    debug_measurement = config.get("debug_measurement")
    if not isinstance(debug_measurement, dict):
        raise RuntimeError(
            "no ongoing debug measurement is saved; pass a measurement_id explicitly"
        )
    if debug_measurement.get("status") != "ongoing":
        raise RuntimeError(
            "the saved debug measurement is not ongoing; pass a measurement_id explicitly"
        )
    measurement_id = str(debug_measurement.get("measurement_id", "")).strip()
    if not measurement_id:
        raise RuntimeError(
            "the saved debug measurement has no measurement_id; pass one explicitly"
        )
    return measurement_id


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _discover_built_firmware(role: str, firmware_cfg: dict[str, Any]) -> Path:
    build_root = Path(str(firmware_cfg.get("build_root", ".fw_build")))
    candidates_by_role = {
        "dut": ["ncp_ble_dut_dcdc", "ncp_ble_dut"],
        "dut_dcdc": ["ncp_ble_dut_dcdc"],
        "dut_no_dcdc": ["ncp_ble_dut"],
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


def _sha256_text_file_lf_normalized(path: Path) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(data).hexdigest()


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
    return _sha256_text_file_lf_normalized(config_path)


def _is_mutable_custom_config_name(config: str) -> bool:
    return config.strip().replace("\\", "/").lower() in {"custom", "custom.json"}


def update_custom_test_config(feature: str, assignments: list[str]) -> Path:
    """Apply explicit dotted-path updates to one feature's mutable custom config."""

    config_path = _local_test_config_path(feature, "custom")
    if not config_path.exists():
        raise RuntimeError(f"custom test config does not exist: {config_path}")
    try:
        config_data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"custom test config is not valid JSON: {config_path}") from exc
    if not isinstance(config_data, dict):
        raise RuntimeError(f"custom test config must be a JSON object: {config_path}")

    for assignment in assignments:
        path, separator, raw_value = assignment.partition("=")
        parts = path.split(".")
        if not separator or not raw_value or not path or any(not part for part in parts):
            raise RuntimeError("custom updates must use PATH=VALUE with a dotted object path")
        current: dict[str, Any] = config_data
        for part in parts[:-1]:
            child = current.get(part)
            if not isinstance(child, dict):
                raise RuntimeError(f"custom config path does not exist: {path}")
            current = child
        if parts[-1] not in current:
            raise RuntimeError(f"custom config path does not exist: {path}")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        current[parts[-1]] = value

    config_path.write_text(json.dumps(config_data, indent=4) + "\n", encoding="utf-8")
    return config_path


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
    if request.differential_firmware is not None:
        for variant_name, firmware in (
            ("version1", request.differential_firmware.version1),
            ("version2", request.differential_firmware.version2),
        ):
            for role_image in firmware.role_images:
                path = Path(role_image.image.url)
                if path.exists() and path.is_file():
                    payloads[
                        f"differential_firmware.{variant_name}.role_images.{role_image.project_name}"
                    ] = base64.b64encode(path.read_bytes()).decode("ascii")
            if firmware.xapi_file is not None:
                path = Path(firmware.xapi_file.url)
                if path.exists() and path.is_file():
                    payloads[f"differential_firmware.{variant_name}.xapi_file"] = (
                        base64.b64encode(path.read_bytes()).decode("ascii")
                    )
            if firmware.bootloader_img is not None:
                path = Path(firmware.bootloader_img.url)
                if path.exists() and path.is_file():
                    payloads[f"differential_firmware.{variant_name}.bootloader_img"] = (
                        base64.b64encode(path.read_bytes()).decode("ascii")
                    )
        return payloads
    for key, image in (("dut_img", request.firmware.dut_img), ("tester_img", request.firmware.tester_img)):
        path = Path(image.url)
        if path.exists() and path.is_file():
            payloads[key] = base64.b64encode(path.read_bytes()).decode("ascii")
    for name, image in request.firmware.dut_variants.items():
        path = Path(image.url)
        if path.exists() and path.is_file():
            payloads[f"dut_variant:{name}"] = base64.b64encode(path.read_bytes()).decode("ascii")
    if request.firmware.bootloader_img is not None:
        path = Path(request.firmware.bootloader_img.url)
        if path.exists() and path.is_file():
            payloads["bootloader_img"] = base64.b64encode(path.read_bytes()).decode("ascii")
    if request.firmware.xapi_file is not None:
        path = Path(request.firmware.xapi_file.url)
        if path.exists() and path.is_file():
            payloads["xapi_file"] = base64.b64encode(path.read_bytes()).decode("ascii")
    return payloads


def _print_measurement_result(
    result: MeasurementResult,
    *,
    colors: ColorFormatter | None = None,
) -> None:
    colors = colors or color_formatter("never")
    print(f"{result.measurement_id}: {colors.state(result.state)} {result.progress_percent}%")
    if result.identity is not None:
        print(f"run_name: {result.identity.run_name}")
        print(f"created_at: {result.identity.created_at.isoformat()}")
    if result.message:
        print(result.message)
    for artifact in result.artifacts:
        location = artifact.url or str(artifact.path or "")
        print(f"artifact {artifact.name}: {location}")


def _print_staged_firmware(payload: dict[str, Any]) -> None:
    """Render the read-only current-stage view without exposing stage IDs."""

    for stage_name in ("normal", "differential"):
        stage = payload.get(stage_name)
        if not isinstance(stage, dict):
            print(f"{stage_name}: none")
            continue
        print(f"{stage_name}: {stage.get('intended_use', '')}")
        print(f"  created_at: {stage.get('created_at', '')}")
        target = stage.get("target", {})
        if isinstance(target, dict):
            print(f"  target: {target.get('board', '')} {target.get('device', '')}".rstrip())
        if stage_name == "differential":
            for variant_name in ("version1", "version2"):
                variant = stage.get(variant_name, {})
                if isinstance(variant, dict):
                    _print_staged_firmware_variant(variant_name, variant)
        else:
            _print_staged_firmware_variant("firmware", stage)


def _print_staged_firmware_variant(label: str, stage: dict[str, Any]) -> None:
    sisdk = stage.get("sisdk", {})
    if isinstance(sisdk, dict):
        print(
            f"  {label} SiSDK: {sisdk.get('version', '')} "
            f"{sisdk.get('commit', '')}".rstrip()
        )
    firmware = stage.get("firmware", [])
    if isinstance(firmware, list):
        names = ", ".join(
            f"{item.get('role', '')}={item.get('name', '')}"
            for item in firmware
            if isinstance(item, dict)
        )
        print(f"  {label} firmware: {names}")


def _local_parameters_path(feature: str) -> Path:
    feature_name = feature.strip()
    if not feature_name:
        raise RuntimeError("test feature is required")
    if Path(feature_name).name != feature_name or feature_name in {".", ".."}:
        raise RuntimeError("test feature must be a single feature name")
    parameters_path = Path("tests") / "configs" / "ble" / feature_name / "parameters.json"
    if not parameters_path.is_file():
        raise RuntimeError(f"test feature parameters do not exist: {parameters_path}")
    return parameters_path


def _run_test_feature(
    client: CMSClient,
    args: argparse.Namespace,
    variable_filters: tuple[VariableFilter, ...],
    colors: ColorFormatter,
) -> CMSCLIExitCode:
    """Submit, observe, and collect artifacts for one feature measurement."""

    result = client.run_test(
        MeasurementMode(args.mode),
        args.feature,
        config=args.config_name,
        run_name=args.run_name,
        variable_filters=variable_filters,
        upload_results_to_database=args.upload_results_to_database,
        debug_artifact_streaming=args.debug,
    )
    record_last_measurement(
        args.config,
        result,
        mode=MeasurementMode(args.mode),
        feature=args.feature,
        config_name=args.config_name,
        run_name=args.run_name,
        variable_filters=variable_filters,
        endpoint=getattr(client, "base_url", ""),
    )
    if not result.measurement_id:
        _print_measurement_result(result, colors=colors)
        return _measurement_exit_code(result.state)
    _record_last_measurement(args, result.measurement_id, variable_filters)
    if args.debug:
        _record_debug_measurement(args, result.measurement_id, variable_filters)
    state = _watch_remote_measurement(
        client,
        result.measurement_id,
        verbose=args.verbose,
        poll_interval_s=args.poll_interval,
        colors=colors,
    )
    result = client.get_measurement(result.measurement_id)
    downloaded_paths: list[Path] = []
    if result.artifacts:
        downloaded_paths = client.download_measurement_artifacts(
            result.measurement_id,
            Path(args.out_dir),
        )
        for path in downloaded_paths:
            print(f"saved {path}")
    execution_state = state.summary.state if state.summary is not None else result.state
    execution_exit = _measurement_exit_code(execution_state)
    if execution_exit is not CMSCLIExitCode.SUCCESS:
        return execution_exit
    if args.mode == MeasurementMode.DIFF.value:
        return _create_and_render_diff_comparison(Path(args.out_dir), downloaded_paths)
    return execution_exit


def _create_and_render_diff_comparison(
    out_dir: Path, downloaded_paths: list[Path]
) -> CMSCLIExitCode:
    """Create client-side differential output only after physical execution passed."""

    plan_paths = [path for path in downloaded_paths if path.name == "paired_default_plan.json"]
    if len(plan_paths) != 1:
        print("differential comparison data did not include one paired_default_plan.json", file=sys.stderr)
        return CMSCLIExitCode.COMPARISON_DATA_FAILURE
    evidence_root = plan_paths[0].parent
    plan_path = evidence_root / "paired_default_plan.json"
    version1_root = evidence_root / "version1"
    version2_root = evidence_root / "version2"
    required = (plan_path, version1_root, version2_root)
    if not all(path.exists() for path in required):
        missing = ", ".join(str(path) for path in required if not path.exists())
        print(f"differential comparison data is incomplete: missing {missing}", file=sys.stderr)
        return CMSCLIExitCode.COMPARISON_DATA_FAILURE
    try:
        pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)
        inputs = prepare_comparison_inputs(pairing)
        report = create_diff_comparison_report(pairing, inputs)
        report_path = write_diff_comparison_report(report, evidence_root / "diff_comparison.json")
        print(f"saved {report_path}")
        saved_report = json.loads(report_path.read_text(encoding="utf-8"))
        print(render_diff_comparison_table(saved_report))
    except (DifferentialPairingError, ValueError, OSError) as exc:
        print(f"differential comparison data is invalid: {exc}", file=sys.stderr)
        return CMSCLIExitCode.COMPARISON_DATA_FAILURE

    if report["diagnostics"]:
        return CMSCLIExitCode.COMPARISON_DATA_FAILURE
    statuses = {
        metric.get("status")
        for pair in report["pairs"]
        for metric in pair["metrics"]
    }
    if ComparisonStatus.FAIL.value in statuses:
        return CMSCLIExitCode.THRESHOLD_REGRESSION
    return CMSCLIExitCode.SUCCESS


def _preview_job_count(
    args: argparse.Namespace,
    variable_filters: tuple[VariableFilter, ...],
) -> int:
    from test_orchestrator import build_jobs

    config_root = Path("tests") / "configs" / "ble"
    feature_filters = set()
    if args.feature != "all":
        config_root /= args.feature
        feature_filters.add(args.feature)
    exact_config = "" if args.config_name == "default" else args.config_name
    discovery_mode = "all" if exact_config else args.mode
    jobs = build_jobs(
        [config_root],
        mode=discovery_mode,
        feature_filters=feature_filters,
        partition_filters=set(),
        exact_config=exact_config,
        variable_filters=variable_filters,
    )
    return len(jobs)


def _print_test_preview(
    args: argparse.Namespace,
    variable_filters: tuple[VariableFilter, ...] = (),
) -> None:
    all_features = args.feature == "all"
    config_path = (
        Path("tests") / "configs" / "ble"
        if all_features
        else _local_test_config_path(args.feature, args.config_name)
    )
    config_hash = "" if all_features else _validate_local_test_config(args.feature, args.config_name)
    parameters_path = None if all_features else _local_parameters_path(args.feature)
    if parameters_path is not None:
        validate_variable_filters(parameters_path, variable_filters)
    expected_job_count = _preview_job_count(args, variable_filters)
    print("test preview")
    print(f"mode: {args.mode}")
    print(f"feature: {args.feature}")
    print(f"config: {args.config_name}")
    print(f"config_path: {config_path}")
    if config_hash:
        print(f"config_hash: {config_hash}")
    if variable_filters:
        print(
            "variables: "
            + ", ".join(f"{item.name}={item.value}" for item in variable_filters)
        )
    print(f"expected_jobs: {expected_job_count}")
    if parameters_path is not None:
        print("available_variables:")
        for line in variable_help_text(parameters_path).splitlines():
            print(f"  {line}")
    if args.run_name:
        print(f"run_name: {args.run_name}")
    print("results_destination: database")


def _failed_test_records_from_job_results(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    failed: list[dict[str, str]] = []
    for record in records:
        if record.get("record_type") != "job_completed":
            continue
        if record.get("status") != "failed":
            continue
        failed.append(
            {
                "job_id": str(record.get("job_id", "")),
                "feature": str(record.get("feature", "")),
                "config": str(record.get("config", "")),
                "mode": str(record.get("mode", "")),
                "partition": str(record.get("partition", "")),
                "config_hash": str(record.get("config_hash", "")),
            }
        )
    return failed


def _rerun_command(failed_test: dict[str, str]) -> str:
    feature = failed_test["feature"]
    config = Path(failed_test["config"]).stem
    return f"cms TEST --mode default --feature {feature} --config-name {config}"


def _print_failed_tests(
    measurement_id: str,
    failed_tests: list[dict[str, str]],
    *,
    as_json: bool = False,
) -> None:
    records = [
        {
            **failed_test,
            "rerun_command": _rerun_command(failed_test),
        }
        for failed_test in failed_tests
    ]
    if as_json:
        print(
            json.dumps(
                {
                    "measurement_id": measurement_id,
                    "failed_tests": records,
                },
                indent=2,
            )
        )
        return

    print(f"failed tests for {measurement_id}: {len(records)}")
    if not records:
        return
    for index, failed_test in enumerate(records, start=1):
        partition = failed_test["partition"] or "-"
        print(
            f"{index}. {failed_test['feature']} {failed_test['config']} "
            f"mode={failed_test['mode'] or '-'} partition={partition} "
            f"hash={failed_test['config_hash'] or '-'}"
        )
        print(f"   rerun: {failed_test['rerun_command']}")


def _print_operator_recipes(args: argparse.Namespace) -> None:
    feature = args.feature
    config_name = args.config_name
    measurement_id = args.measurement_id
    cursor = args.cursor
    out_dir = args.out_dir
    print("Default config:")
    print(f"  cms TEST --mode default --feature {feature} --config-name default --preview")
    print(f"  cms TEST --mode default --feature {feature} --config-name default --out-dir {out_dir}")
    print()
    print("Exact named config:")
    print(f"  cms TEST --mode default --feature {feature} --config-name {config_name} --preview")
    print(f"  cms TEST --mode default --feature {feature} --config-name {config_name} --out-dir {out_dir}")
    print()
    print("Attach or reconnect:")
    print("  cms WATCH")
    print(f"  cms WATCH {measurement_id} --cursor 0")
    print(f"  cms WATCH {measurement_id} --cursor {cursor}")
    print(f"  cms WATCH {measurement_id} --cursor {cursor} --verbose")
    print()
    print("Cancel the real run:")
    print("  cms CANCEL")
    print("  cms WATCH")
    print()
    print("Find failed tests to rerun:")
    print(f"  cms FAILED {measurement_id}")
    print(f"  cms FAILED {measurement_id} --json")
    print()
    print("Download artifacts by measurement ID:")
    print(f"  cms ARTIFACTS {measurement_id} --out-dir {out_dir}")


def _print_exit_codes() -> None:
    descriptions = {
        CMSCLIExitCode.SUCCESS: "command succeeded or watched measurement passed",
        CMSCLIExitCode.TEST_FAILED: "completed test failure",
        CMSCLIExitCode.USAGE_ERROR: "client usage error",
        CMSCLIExitCode.TEST_CANCELLED: "measurement cancelled",
        CMSCLIExitCode.TEST_REJECTED: "measurement request rejected",
        CMSCLIExitCode.CONNECTION_FAILURE: "connection failure",
        CMSCLIExitCode.PROTOCOL_MISMATCH: "protocol mismatch",
        CMSCLIExitCode.CLIENT_ERROR: "daemon, client, configuration, or operational error",
        CMSCLIExitCode.COMPARISON_DATA_FAILURE: "differential comparison data failure",
        CMSCLIExitCode.THRESHOLD_REGRESSION: "differential comparison threshold regression",
    }
    for code in CMSCLIExitCode:
        print(f"{int(code)} {code.name}: {descriptions[code]}")


def _troubleshooting_topics() -> dict[str, tuple[str, tuple[str, ...]]]:
    return {
        "health": (
            "health warnings",
            (
                "Run cms health first, then read each failed or warning check by name.",
                "Fix daemon_startup, runtime_storage, artifact_storage, bench_config, and tool checks before submitting tests.",
                "A runtime filesystem warning on Windows can be expected for hardware-free development.",
            ),
        ),
        "hardware-mappings": (
            "missing hardware mappings",
            (
                "Check cms_bench_runtime.json for board, device, DUT probe, tester probe, serial ports, and Joulescope mapping.",
                "Run cms health again after editing bench-local mappings.",
                "Do not try to repair bench hardware mappings from cms_client_config.json.",
            ),
        ),
        "firmware": (
            "stale or missing firmware",
            (
                "Run cms BUILD, then cms SEND_FW before cms TEST.",
                "If SEND_FW rejects hashes or xapi provenance, rebuild and restage firmware.",
                "TEST uses the most recently staged firmware; it does not upload firmware by itself.",
            ),
        ),
        "configs": (
            "unavailable named configs",
            (
                "Run cms tests and confirm the feature and config are advertised.",
                "Run cms TEST --mode <mode> --feature <feature> --config-name <name> --preview before submitting.",
                "The client and bench reject missing, malformed, path-traversal, and hash-mismatched configs before hardware execution.",
            ),
        ),
        "daemon": (
            "daemon connection failures",
            (
                "Check cms_client_config.json daemon.base_url or pass --base-url.",
                "Run cms health with the same --base-url and --api-token options you use for TEST.",
                "Exit code 5 means the daemon is unreachable or timed out; exit code 6 means protocol mismatch.",
            ),
        ),
        "truncated-output": (
            "truncated live output",
            (
                "WATCH continues from the oldest retained cursor when verbose console output is truncated.",
                "Structured summary, counts, current test, and failed identities do not depend on retained console text.",
                "Use cms WATCH <measurement_id> --verbose for retained stdout/stderr details.",
            ),
        ),
        "stalled-setup": (
            "stalled hardware setup",
            (
                "Watch lifecycle events with cms WATCH <measurement_id> --verbose.",
                "Look for the last setup step that started without a completion record.",
                "The bench execution timeout should report a terminal failure instead of hanging forever.",
            ),
        ),
        "artifacts": (
            "missing artifacts",
            (
                "Use cms ARTIFACTS <measurement_id> --out-dir artifacts; do not rely on last.",
                "Artifacts are published only after the measurement reaches a terminal state.",
                "Use cms FAILED <measurement_id> when you need failed test identities for rerun.",
            ),
        ),
    }


def _print_troubleshooting(topic: str = "all") -> None:
    topics = _troubleshooting_topics()
    selected = topics.items() if topic == "all" else [(topic, topics[topic])]
    for index, (_key, (title, lines)) in enumerate(selected):
        if index:
            print()
        print(f"{title}:")
        for line in lines:
            print(f"  - {line}")


def _resolve_watch_measurement_id(
    client: CMSClient,
    requested_measurement_id: str,
    *,
    reconnect: bool = False,
    config_path: str | Path = CMS_CLIENT_CONFIG,
) -> str:
    """Resolve an explicit, active, or locally recovered measurement identity."""

    measurement_id = requested_measurement_id.strip()
    if reconnect:
        if measurement_id:
            raise RuntimeError("cms WATCH --reconnect cannot be combined with a measurement ID")
        config_file = Path(config_path)
        if not config_file.is_file():
            raise RuntimeError(
                "cannot reconnect because the CMS client recovery record does not exist; "
                "submit a test first or use cms WATCH <measurement_id>"
            )
        record = _read_json_file(config_file).get("last_measurement")
        if not isinstance(record, dict) or not isinstance(record.get("measurement_id"), str):
            raise RuntimeError(
                "cannot reconnect because no submitted measurement is recorded; "
                "submit a test first or use cms WATCH <measurement_id>"
            )
        measurement_id = record["measurement_id"].strip()
        if not measurement_id:
            raise RuntimeError(
                "cannot reconnect because the recorded measurement ID is empty; "
                "use cms WATCH <measurement_id>"
            )
        return measurement_id
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


def _print_measurement_summary(
    summary: MeasurementSummary,
    *,
    colors: ColorFormatter | None = None,
) -> None:
    colors = colors or color_formatter("never")
    counts = summary.counts
    print(f"{summary.measurement_id}: {colors.state(summary.state)}")
    print(
        "tests: "
        f"total={counts.total} completed={counts.completed} "
        f"passed={colors.color(counts.passed, 'passed')} "
        f"failed={colors.color(counts.failed, 'failed')} "
        f"skipped={colors.color(counts.skipped, 'skipped')} "
        f"remaining={colors.color(counts.remaining, 'running')}"
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


def _print_verbose_event_page(
    page: MeasurementEventPage,
    *,
    colors: ColorFormatter | None = None,
) -> None:
    colors = colors or color_formatter("never")
    for event in page.events:
        timestamp = event.timestamp.isoformat()
        if event.event_type == "console":
            source = str(event.details.get("source", "console"))
            variant = str(event.details.get("variant", ""))
            label = f"{variant} {source}" if variant else source
            text = str(event.details.get("text", ""))
            for line in text.splitlines() or [""]:
                print(f"{colors.color(timestamp, 'dim')} [{label}] {line}")
            continue
        state = str(event.details.get("state", event.event_type))
        print(f"{colors.color(timestamp, 'dim')} [event] {colors.color(state, state_role(state))}")


def _watch_remote_measurement(
    client: CMSClient,
    measurement_id: str,
    *,
    after_cursor: EventCursor = EventCursor(),
    verbose: bool = False,
    poll_interval_s: float = 1.0,
    colors: ColorFormatter | None = None,
) -> MeasurementWatchState:
    colors = colors or color_formatter("never")
    state = MeasurementWatchState(measurement_id, cursor=after_cursor)
    last_summary_key: tuple[Any, ...] | None = None

    def render_summary(summary: MeasurementSummary) -> None:
        nonlocal last_summary_key
        summary_key = _measurement_summary_key(summary)
        if summary_key != last_summary_key:
            _print_measurement_summary(summary, colors=colors)
            last_summary_key = summary_key

    def render_truncation(oldest_retained_cursor: EventCursor) -> None:
        print(
            colors.color("warning:", "warning")
            + " verbose console output was truncated; "
            f"continuing from cursor {oldest_retained_cursor.value}"
        )

    return watch_measurement(
        client,
        state,
        poll_interval_s=poll_interval_s,
        on_summary=render_summary,
        on_events=(lambda page: _print_verbose_event_page(page, colors=colors)) if verbose else None,
        on_truncation=render_truncation,
    )


if __name__ == "__main__":
    sys.exit(main())

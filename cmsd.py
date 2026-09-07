"""CMS daemon bringup for client-to-bench communication.

`cmsd.py` owns the network-facing daemon and daemon-side staging area. The
daemon initializes one local `CMSBench`, exposes health and status over HTTP,
and keeps request/runtime files inside daemon-owned directories.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import shutil
import tempfile
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from cms_bench import (
    DifferentialCampaignSession,
    DEFAULT_CONSOLE_EVENT_MAX_BYTES,
    DEFAULT_CONSOLE_LOG_MAX_BYTES,
    DEFAULT_EXECUTION_TIMEOUT_S,
    BenchSession,
    CMSBench,
    CMS_BENCH_CONFIG,
    MeasurementEventStream,
    MeasurementRuntimePaths,
    load_bench_runtime_config,
)
from cms_protocol import (
    CMS_OBSERVATION_PROTOCOL_VERSION,
    Artifacts,
    BenchCheckResult,
    BenchCheckStatus,
    BenchHealthReport,
    BenchState,
    EventCursor,
    MeasurementEventPage,
    MeasurementMode,
    MeasurementSummary,
    MeasurementState,
    MeasurementRequest,
    MeasurementResult,
    ResultType,
    TestCaseIdentity,
    TestCaseState,
    TestCounts,
    VariableFilter,
    bench_health_report_to_dict,
    bench_identity_to_dict,
    create_measurement_identity,
    differential_firmware_to_dict,
    measurement_event_from_dict,
    measurement_event_page_to_dict,
    measurement_request_from_dict,
    measurement_request_to_dict,
    measurement_result_from_dict,
    measurement_result_to_dict,
    measurement_summary_to_dict,
    test_case_identity_from_dict,
    variable_filters_from_dicts,
)


PUBLISHABLE_DIAGNOSTIC_NAMES = frozenset(
    {
        "cms_fw_metadata.json",
        "cms_test_run.json",
        "orchestrator_plan.json",
        "orchestrator_results.jsonl",
        "pytest.log",
        "test_stdout.log",
        "test_stderr.log",
    }
)


def _should_upload_results_to_database(mode: MeasurementMode | str) -> bool:
    measurement_mode = mode if isinstance(mode, MeasurementMode) else MeasurementMode(str(mode))
    return measurement_mode is not MeasurementMode.DEBUG


def _default_runtime_root() -> Path:
    if platform.system() == "Linux":
        return Path("/dev/shm/cmsd")
    return Path(tempfile.gettempdir()) / "cmsd"


def _default_execution_timeout_s() -> float | None:
    value = os.getenv("CMSD_EXECUTION_TIMEOUT_S", "").strip().lower()
    if not value or value in {"0", "none", "off", "unlimited"}:
        return DEFAULT_EXECUTION_TIMEOUT_S
    return float(value)


@dataclass(slots=True)
class DaemonConfig:
    """Configuration needed to run one daemon instance."""

    host: str = "0.0.0.0"
    port: int = 8000
    runtime_root: Path = field(default_factory=_default_runtime_root)
    artifacts_root: Path = Path("artifacts/cmsd")
    bench_config: Path = Path(CMS_BENCH_CONFIG)
    api_token: str = ""
    expected_runtime_filesystems: tuple[str, ...] = ("tmpfs", "ramfs")
    max_console_log_bytes: int = DEFAULT_CONSOLE_LOG_MAX_BYTES
    max_console_event_bytes: int = DEFAULT_CONSOLE_EVENT_MAX_BYTES
    execution_timeout_s: float | None = field(default_factory=_default_execution_timeout_s)
    completed_measurement_retention: int = 100

    def __post_init__(self) -> None:
        runtime_root = self.runtime_root.resolve()
        artifacts_root = self.artifacts_root.resolve()
        if (
            runtime_root == artifacts_root
            or runtime_root.is_relative_to(artifacts_root)
            or artifacts_root.is_relative_to(runtime_root)
        ):
            raise ValueError("runtime_root and artifacts_root must not overlap")
        if self.max_console_log_bytes < 1:
            raise ValueError("max_console_log_bytes must be positive")
        if self.max_console_event_bytes < 512:
            raise ValueError("max_console_event_bytes must be at least 512")
        if self.execution_timeout_s is not None and self.execution_timeout_s <= 0:
            raise ValueError("execution_timeout_s must be positive when configured")
        if self.completed_measurement_retention < 1:
            raise ValueError("completed_measurement_retention must be positive")


@dataclass(slots=True)
class DaemonFiles:
    """Daemon-owned files visible before and during bench execution."""

    result_json: Path
    result_npz: Path
    dut_image: Path
    dut_image_hash: Path
    tester_image: Path
    tester_image_hash: Path
    measurement_request: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "result_json": str(self.result_json),
            "result_npz": str(self.result_npz),
            "dut_image": str(self.dut_image),
            "dut_image_hash": str(self.dut_image_hash),
            "tester_image": str(self.tester_image),
            "tester_image_hash": str(self.tester_image_hash),
            "measurement_request": str(self.measurement_request),
        }


@dataclass(frozen=True, slots=True)
class StagedDifferentialFirmware:
    """A complete validated parent/child pair retained under one staging ID."""

    request: MeasurementRequest
    runtime_dir: Path
    manifest_path: Path


class CMSDaemon:
    """Network service that brokers one active remote measurement for one bench."""

    def __init__(self, config: DaemonConfig | None = None):
        self.config = config or DaemonConfig()
        self.active_measurement_id: str | None = None
        self.files = self._daemon_files()
        self.initialized = False
        self.startup_error = ""
        self.bench: CMSBench | None = None
        self.measurements: dict[str, MeasurementResult] = {}
        self.last_firmware_request: MeasurementRequest | None = None
        self.last_firmware_runtime_dir: Path | None = None
        self.staged_differential_firmware: dict[str, StagedDifferentialFirmware] = {}
        self.last_measurement_id: str | None = None
        self.cancelled_measurements: set[str] = set()
        self.cancellation_events: dict[str, threading.Event] = {}

    def initialize(self) -> None:
        """Prepare daemon runtime directories, staging files, and local bench."""

        try:
            self.config.runtime_root.mkdir(parents=True, exist_ok=True)
            self.config.artifacts_root.mkdir(parents=True, exist_ok=True)
            self._touch_staging_files()
            self.reconstruct_completed_results()
            self.reconstruct_interrupted_runtime_workspaces()

            identity, resources = load_bench_runtime_config(self.config.bench_config)
            self.bench = CMSBench(
                identity=identity,
                resources=resources,
                config_path=self.config.bench_config,
            )
            self.bench.initialize()

            self.initialized = True
            self.startup_error = ""
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.initialized = False
            self.startup_error = str(exc)
            self.bench = CMSBench(config_path=self.config.bench_config)
            self.bench.state = BenchState.ERROR
            self.bench.startup_error = self.startup_error

    def create_runtime_workspace(self, measurement_id: str) -> Path:
        """Create the runtime workspace where incoming firmware is staged."""

        workspace = self._runtime_workspace_path(measurement_id)
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def fw_fetch(self, request: MeasurementRequest, runtime_dir: Path) -> None:
        """Stage or fetch the requested firmware payloads into the runtime area."""
        _stage_firmware_image(request.firmware.dut_img.url, runtime_dir / request.firmware.dut_img.name)
        _stage_firmware_image(request.firmware.tester_img.url, runtime_dir / request.firmware.tester_img.name)
        for image in request.firmware.dut_variants.values():
            _stage_firmware_image(image.url, runtime_dir / image.name)
        if request.firmware.xapi_file is not None:
            _stage_firmware_image(
                request.firmware.xapi_file.url,
                runtime_dir / request.firmware.xapi_file.name,
            )
        if request.firmware.bootloader_img is not None:
            _stage_firmware_image(
                request.firmware.bootloader_img.url,
                runtime_dir / request.firmware.bootloader_img.name,
            )

    def get_health(self) -> BenchHealthReport:
        """Return a structured daemon-plus-bench health report."""

        bench_report = self._bench_health()
        checks = list(bench_report.checks)
        checks.extend(self._daemon_checks())

        if self.startup_error:
            checks.append(
                BenchCheckResult(
                    name="daemon_startup",
                    status=BenchCheckStatus.FAIL,
                    summary="daemon startup failed",
                    detail=self.startup_error,
                )
            )
        else:
            checks.append(
                BenchCheckResult(
                    name="daemon_startup",
                    status=BenchCheckStatus.PASS if self.initialized else BenchCheckStatus.FAIL,
                    summary="daemon initialized" if self.initialized else "daemon not initialized",
                )
            )

        failed = [check.name for check in checks if check.status is BenchCheckStatus.FAIL]
        warned = [check.name for check in checks if check.status is BenchCheckStatus.WARN]
        state = BenchState.ERROR if failed else bench_report.state
        if state is BenchState.TESTING:
            state = BenchState.BUSY

        summary = bench_report.summary
        if failed:
            summary = "daemon health failed: " + ", ".join(failed)
        elif warned:
            summary = "daemon health warning: " + ", ".join(warned)

        return BenchHealthReport(
            identity=bench_report.identity,
            state=state,
            dut_state=bench_report.dut_state,
            tester_state=bench_report.tester_state,
            js_state=bench_report.js_state,
            summary=summary,
            checks=checks,
        )

    def get_status(self) -> dict[str, Any]:
        """Return daemon state, bench state, and staged file paths."""

        health = self.get_health()
        return {
            "daemon_initialized": self.initialized,
            "startup_error": self.startup_error,
            "bench_launched": self.bench is not None,
            "identity": bench_identity_to_dict(health.identity),
            "bench_state": health.state.value,
            "active_measurement_id": self.active_measurement_id,
            "runtime_root": str(self.config.runtime_root),
            "artifacts_root": str(self.config.artifacts_root),
            "files": self.files.as_dict(),
        }

    def get_active_measurement_id(self) -> str | None:
        """Return the daemon-authoritative active measurement, if any."""

        return self.active_measurement_id

    def get_measurement_summary(self, measurement_id: str) -> MeasurementSummary:
        """Reconstruct concise job state from the runtime plan and result log."""

        plan_path, results_path = self._observation_run_file_paths(measurement_id)
        plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
        plan_jobs = plan.get("jobs", [])
        if not isinstance(plan_jobs, list):
            raise ValueError("orchestrator plan jobs must be a list")

        identities: dict[str, TestCaseIdentity] = {}
        ordered_job_ids: list[str] = []
        states: dict[str, TestCaseState] = {}
        for job in plan_jobs:
            identity = test_case_identity_from_dict(job)
            if identity.job_id in identities:
                raise ValueError(f"orchestrator plan contains duplicate job id: {identity.job_id}")
            identities[identity.job_id] = identity
            ordered_job_ids.append(identity.job_id)
            states[identity.job_id] = TestCaseState.PENDING

        current_job_id: str | None = None
        for record in _read_jsonl_records(results_path):
            job_id = str(record.get("job_id", ""))
            if not job_id or job_id not in identities:
                continue
            status = TestCaseState(str(record.get("status", TestCaseState.PENDING.value)))
            states[job_id] = status
            if status is TestCaseState.RUNNING:
                current_job_id = job_id
            elif current_job_id == job_id:
                current_job_id = None

        terminal_states = {
            TestCaseState.PASSED,
            TestCaseState.FAILED,
            TestCaseState.SKIPPED,
            TestCaseState.CANCELLED,
        }
        counts = TestCounts(
            total=len(ordered_job_ids),
            completed=sum(states[job_id] in terminal_states for job_id in ordered_job_ids),
            passed=sum(states[job_id] is TestCaseState.PASSED for job_id in ordered_job_ids),
            failed=sum(states[job_id] is TestCaseState.FAILED for job_id in ordered_job_ids),
            skipped=sum(states[job_id] is TestCaseState.SKIPPED for job_id in ordered_job_ids),
        )
        tracked_result = self._observation_measurement_result(measurement_id)
        state = tracked_result.state if tracked_result is not None else MeasurementState.RUNNING
        identity = tracked_result.identity if tracked_result is not None else None
        return MeasurementSummary(
            measurement_id=measurement_id,
            identity=identity,
            state=state,
            counts=counts,
            current_test=identities.get(current_job_id or ""),
            failed_tests=tuple(
                identities[job_id]
                for job_id in ordered_job_ids
                if states[job_id] is TestCaseState.FAILED
            ),
        )

    def get_measurement_events(
        self,
        measurement_id: str,
        after_cursor: EventCursor = EventCursor(),
    ) -> MeasurementEventPage:
        """Return retained structured and console events after one cursor."""

        runtime_dir = self._runtime_workspace_path(measurement_id)
        if not runtime_dir.is_dir():
            if not self._published_measurement_path(measurement_id).is_dir() and measurement_id in self.measurements:
                return MeasurementEventPage(
                    measurement_id=measurement_id,
                    events=(),
                    next_cursor=after_cursor,
                    oldest_retained_cursor=EventCursor(),
                    truncated=False,
                )
            return self._get_published_measurement_events(measurement_id, after_cursor)

        stream = MeasurementEventStream(
            measurement_id,
            MeasurementRuntimePaths.from_root(runtime_dir),
            max_console_bytes=self.config.max_console_event_bytes,
        )
        retained_records = stream.read_events()
        events = tuple(
            measurement_event_from_dict(record)
            for record in retained_records
            if int(record.get("cursor", 0)) > after_cursor.value
        )
        next_cursor = events[-1].cursor if events else after_cursor
        console_was_truncated = any(
            record.get("type") == "lifecycle"
            and record.get("state") == "console_truncated"
            for record in retained_records
        )
        if console_was_truncated:
            oldest_retained_cursor = EventCursor(stream.oldest_retained_cursor)
        else:
            oldest_retained_cursor = EventCursor(
                min(
                    (int(record.get("cursor", 0)) for record in retained_records),
                    default=0,
                )
            )
        return MeasurementEventPage(
            measurement_id=measurement_id,
            events=events,
            next_cursor=next_cursor,
            oldest_retained_cursor=oldest_retained_cursor,
            truncated=(
                console_was_truncated
                and
                oldest_retained_cursor.value > 0
                and after_cursor.value < oldest_retained_cursor.value - 1
            ),
        )

    def _get_published_measurement_events(
        self,
        measurement_id: str,
        after_cursor: EventCursor,
    ) -> MeasurementEventPage:
        published_dir = self._published_measurement_path(measurement_id)
        if not published_dir.is_dir():
            raise KeyError(f"measurement id is unknown: {measurement_id}")
        records = _published_event_records_from_results(
            measurement_id,
            published_dir / "orchestrator_results.jsonl",
        )
        events = tuple(
            measurement_event_from_dict(record)
            for record in records
            if int(record.get("cursor", 0)) > after_cursor.value
        )
        next_cursor = events[-1].cursor if events else after_cursor
        oldest_retained_cursor = EventCursor(
            min((int(record.get("cursor", 0)) for record in records), default=0)
        )
        return MeasurementEventPage(
            measurement_id=measurement_id,
            events=events,
            next_cursor=next_cursor,
            oldest_retained_cursor=oldest_retained_cursor,
            truncated=False,
        )

    def get_measurement_result(self, measurement_id: str) -> MeasurementResult:
        """Return measurement status from the authoritative active or completed store."""

        if self.active_measurement_id == measurement_id:
            status_path = self._runtime_workspace_path(measurement_id) / "current_status.json"
            if status_path.exists():
                return _measurement_result_from_runtime_status(
                    measurement_id,
                    json.loads(status_path.read_text(encoding="utf-8")),
                    fallback=self.measurements.get(measurement_id),
                )

        result_path = self.config.artifacts_root / "measurements" / measurement_id / "measurement_result.json"
        if result_path.exists():
            return measurement_result_from_dict(json.loads(result_path.read_text(encoding="utf-8")))

        result = self.measurements.get(measurement_id)
        if result is not None:
            return result

        return MeasurementResult(
            measurement_id=measurement_id,
            state=MeasurementState.FAILED,
            progress_percent=100,
            current_step=MeasurementState.FAILED.value,
            message="measurement id is unknown",
        )

    def reconstruct_completed_results(self) -> None:
        """Load completed measurement results from published artifact directories."""

        measurements_root = self.config.artifacts_root / "measurements"
        if not measurements_root.is_dir():
            return

        reconstructed: list[MeasurementResult] = []
        for result_path in sorted(measurements_root.glob("*/measurement_result.json")):
            try:
                result = measurement_result_from_dict(
                    json.loads(result_path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if result.measurement_id:
                reconstructed.append(result)

        reconstructed = sorted(
            reconstructed,
            key=_measurement_result_sort_key,
            reverse=True,
        )
        retained = reconstructed[: self.config.completed_measurement_retention]
        for result in retained:
            self.measurements[result.measurement_id] = result
        if retained:
            self.last_measurement_id = retained[0].measurement_id

    def completed_measurements(self) -> list[MeasurementResult]:
        """Return locally discoverable completed results, newest first."""

        completed_states = {
            MeasurementState.PASSED,
            MeasurementState.FAILED,
            MeasurementState.CANCELLED,
            MeasurementState.REJECTED,
            MeasurementState.INTERRUPTED,
        }
        return sorted(
            (
                result
                for result in self.measurements.values()
                if result.state in completed_states
            ),
            key=_measurement_result_sort_key,
            reverse=True,
        )[: self.config.completed_measurement_retention]

    def reconstruct_interrupted_runtime_workspaces(self) -> None:
        """Mark unfinished runtime workspaces after daemon restart.

        A restarted daemon cannot safely manage an already-running child process,
        so an unfinished RAM workspace is terminally marked as interrupted. A
        published final result remains authoritative and is never overwritten.
        """

        measurements_root = self.config.runtime_root / "measurements"
        if not measurements_root.is_dir():
            return

        for runtime_dir in sorted(path for path in measurements_root.iterdir() if path.is_dir()):
            measurement_id = runtime_dir.name
            try:
                self._runtime_workspace_path(measurement_id)
                published_result = self._published_measurement_path(measurement_id) / "measurement_result.json"
            except ValueError:
                continue
            if published_result.exists() or not _runtime_workspace_has_run_evidence(runtime_dir):
                continue

            status = _read_optional_json_object(runtime_dir / "current_status.json")
            result = _interrupted_result_from_runtime_status(measurement_id, status)
            self.measurements[measurement_id] = result
            if self.active_measurement_id == measurement_id:
                self.active_measurement_id = None
            self.cancellation_events.pop(measurement_id, None)
            self.cancelled_measurements.discard(measurement_id)
            try:
                stream = MeasurementEventStream(
                    measurement_id,
                    MeasurementRuntimePaths.from_root(runtime_dir),
                    max_console_bytes=self.config.max_console_event_bytes,
                )
                stream.append_lifecycle(
                    "daemon_restart_interrupted",
                    message=result.message,
                )
                stream.replace_status(
                    {
                        "state": MeasurementState.INTERRUPTED.value,
                        "progress_percent": result.progress_percent,
                        "current_step": result.current_step,
                        "message": result.message,
                    }
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
            self.publish_results(result)

    def measurement_accept(self, request: MeasurementRequest) -> dict[str, Any]:
        """Stage the current measurement request without launching execution."""

        self.active_measurement_id = request.request_id
        self.cancellation_events.setdefault(request.request_id, threading.Event())
        self.measurements[request.request_id] = MeasurementResult(
            measurement_id=request.request_id,
            state=MeasurementState.ACCEPTED,
            started_at=request.identity.created_at if request.identity else datetime.utcnow(),
            progress_percent=5,
            current_step=MeasurementState.ACCEPTED.value,
            message="measurement accepted",
            identity=request.identity,
        )
        self.files.measurement_request.write_text(
            json.dumps(measurement_request_to_dict(request), indent=2) + "\n",
            encoding="utf-8",
        )
        return self.get_status()

    def stage_firmware(
        self,
        request: MeasurementRequest,
        image_payloads: dict[str, str] | None = None,
    ) -> MeasurementResult:
        """Stage firmware and metadata on the bench without starting a test."""

        runtime_dir = self.create_runtime_workspace(request.request_id)
        self.measurement_accept(request)
        try:
            self._update_measurement(
                request.request_id,
                MeasurementState.WAITING_FOR_ARTIFACTS,
                20,
                "staging firmware artifacts",
            )
            if request.differential_firmware is not None:
                if not image_payloads:
                    raise RuntimeError("differential firmware staging requires one complete image payload request")
                _write_differential_firmware_payloads(request, runtime_dir, image_payloads)
                if self.bench is None:
                    self.bench = CMSBench(config_path=self.config.bench_config)
                    self.bench.initialize()
                self._update_measurement(
                    request.request_id,
                    MeasurementState.VALIDATING_FIRMWARE,
                    60,
                    "validating staged differential firmware",
                )
                self.bench.validate_differential_firmware(request, runtime_dir)
                staging_artifact = self._retain_staged_differential_firmware(request, runtime_dir)
                result = MeasurementResult(
                    measurement_id=request.request_id,
                    state=MeasurementState.ACCEPTED,
                    started_at=self.measurements[request.request_id].started_at,
                    progress_percent=100,
                    current_step=MeasurementState.ACCEPTED.value,
                    message="differential firmware staged and bench validated",
                    artifacts=[staging_artifact],
                )
            elif image_payloads:
                _write_firmware_payloads(request, runtime_dir, image_payloads)
            else:
                self.fw_fetch(request, runtime_dir)
            if request.differential_firmware is None:
                session = self.build_session(request, runtime_dir)
                if self.bench is None:
                    self.bench = CMSBench(config_path=self.config.bench_config)
                    self.bench.initialize()
                self._update_measurement(
                    request.request_id,
                    MeasurementState.VALIDATING_FIRMWARE,
                    60,
                    "validating staged firmware",
                )
                if not self.bench.fw_is_valid(session):
                    raise RuntimeError(_validation_failure_message(session))
                self.bench.persist_staged_firmware_provenance(session)
                result = MeasurementResult(
                    measurement_id=request.request_id,
                    state=MeasurementState.ACCEPTED,
                    started_at=self.measurements[request.request_id].started_at,
                    progress_percent=100,
                    current_step=MeasurementState.ACCEPTED.value,
                    message="firmware staged and ready for test",
                )
                self.last_firmware_request = request
                self.last_firmware_runtime_dir = runtime_dir
                if self.active_measurement_id == request.request_id:
                    self.active_measurement_id = None
        except (OSError, RuntimeError, ValueError) as exc:
            result = MeasurementResult(
                measurement_id=request.request_id,
                state=MeasurementState.FAILED,
                started_at=self.measurements[request.request_id].started_at,
                finished_at=datetime.utcnow(),
                progress_percent=100,
                current_step=MeasurementState.FAILED.value,
                message=str(exc),
            )
        self.measurements[request.request_id] = result
        if self.active_measurement_id == request.request_id:
            self.active_measurement_id = None
        self.cancellation_events.pop(request.request_id, None)
        return result

    def _retain_staged_differential_firmware(
        self,
        request: MeasurementRequest,
        runtime_dir: Path,
    ) -> Artifacts:
        """Make one complete validated differential pair addressable by its staging ID."""

        manifest_path = runtime_dir / "firmware" / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError(f"differential firmware staging manifest is missing: {manifest_path}")
        self.staged_differential_firmware[request.request_id] = StagedDifferentialFirmware(
            request=request,
            runtime_dir=runtime_dir,
            manifest_path=manifest_path,
        )
        return Artifacts(
            name=f"cmsd/{request.request_id}/firmware/manifest.json",
            path=manifest_path,
            result_type=ResultType.DIAGNOSTIC,
        )

    def get_staged_firmware(self, staging_id: str) -> "StagedDifferentialFirmware | None":
        """Find a validated differential pair by its explicit staging ID."""

        return self.staged_differential_firmware.get(staging_id)

    def differential_result_artifacts(self, runtime_dir: Path) -> list[Artifacts]:
        """Expose retained parent and child raw evidence independently."""

        artifacts: list[Artifacts] = []
        for variant in ("parent", "child"):
            result_root = runtime_dir / "results" / variant
            if not result_root.is_dir():
                continue
            for path in sorted(result_root.rglob("*")):
                if path.is_file():
                    artifacts.append(
                        Artifacts(
                            name=(Path("results") / variant / path.relative_to(result_root)).as_posix(),
                            path=path,
                            result_type=ResultType.DIAGNOSTIC,
                        )
                    )
        return artifacts

    def run_staged_test(
        self,
        *,
        mode: str,
        feature: str,
        config: str = "default",
        config_hash: str = "",
        run_name: str = "",
        upload_results_to_database: bool = True,
        debug_artifact_streaming: bool = False,
    ) -> MeasurementResult:
        """Run a test using the most recently staged firmware request."""

        if self.last_firmware_request is None:
            return MeasurementResult(
                measurement_id="",
                state=MeasurementState.FAILED,
                finished_at=datetime.utcnow(),
                progress_percent=100,
                current_step=MeasurementState.FAILED.value,
                message="no firmware has been staged; run SEND_FW first",
            )
        request = measurement_request_from_dict(measurement_request_to_dict(self.last_firmware_request))
        request.mode = request.mode.__class__(mode)
        request.selector.mode = "ble"
        request.selector.feature = feature
        request.selector.config = config
        request.selector.config_hash = config_hash
        request.upload_results_to_database = _should_upload_results_to_database(request.mode)
        request.debug_artifact_streaming = debug_artifact_streaming
        request.identity = None
        request.request_id = ""
        self._assign_execution_identity(request, run_name)
        result = self.execute_measurement(request, identity_assigned=True)
        self.last_measurement_id = result.measurement_id
        return result

    def start_staged_test(
        self,
        *,
        mode: str,
        feature: str,
        config: str = "default",
        config_hash: str = "",
        variable_filters: tuple[VariableFilter, ...] = (),
        run_name: str = "",
        upload_results_to_database: bool = True,
        debug_artifact_streaming: bool = False,
        staging_id: str = "",
    ) -> MeasurementResult:
        """Start a test in the background using explicitly selected staged firmware."""

        measurement_mode = MeasurementMode(mode)
        differential_staging: StagedDifferentialFirmware | None = None
        if measurement_mode is MeasurementMode.DIFF:
            staging_id = staging_id.strip()
            if not staging_id:
                return _staged_test_failure("cms TEST --mode diff requires --staging-id")
            differential_staging = self.get_staged_firmware(staging_id)
            if differential_staging is None:
                return _staged_test_failure(
                    f"differential firmware staging ID is unknown: {staging_id}"
                )
            plan_builder = self.bench or CMSBench(config_path=self.config.bench_config)
            plan_builder.build_differential_plan(
                differential_staging.request,
                differential_staging.runtime_dir,
            )
            request = measurement_request_from_dict(
                measurement_request_to_dict(differential_staging.request)
            )
        elif staging_id.strip():
            return _staged_test_failure(
                "--staging-id is only valid with cms TEST --mode diff"
            )
        elif self.last_firmware_request is None:
            return MeasurementResult(
                measurement_id="",
                state=MeasurementState.FAILED,
                finished_at=datetime.utcnow(),
                progress_percent=100,
                current_step=MeasurementState.FAILED.value,
                message="no firmware has been staged; run SEND_FW first",
            )
        else:
            request = measurement_request_from_dict(
                measurement_request_to_dict(self.last_firmware_request)
            )
        request.mode = measurement_mode
        request.selector.mode = "ble"
        request.selector.feature = feature
        request.selector.config = config
        request.selector.config_hash = config_hash
        request.selector.variable_filters = variable_filters
        request.upload_results_to_database = _should_upload_results_to_database(request.mode)
        request.debug_artifact_streaming = debug_artifact_streaming
        request.identity = None
        request.request_id = ""
        identity = self._assign_execution_identity(request, run_name)

        started = MeasurementResult(
            measurement_id=request.request_id,
            state=MeasurementState.ACCEPTED,
            started_at=identity.created_at,
            progress_percent=1,
            current_step=MeasurementState.ACCEPTED.value,
            message="test accepted",
            identity=identity,
        )
        self.measurements[request.request_id] = started
        self.active_measurement_id = request.request_id
        self.cancellation_events[request.request_id] = threading.Event()
        thread = threading.Thread(
            target=self.execute_measurement,
            args=(request,),
            kwargs={
                "staged_runtime_dir": (
                    differential_staging.runtime_dir
                    if differential_staging is not None
                    else self.last_firmware_runtime_dir
                ),
                "differential_staging": differential_staging,
                "identity_assigned": True,
            },
            daemon=True,
        )
        thread.start()
        return started

    def build_session(self, request: MeasurementRequest, runtime_dir: Path) -> BenchSession:
        """Assemble the bench execution context after the bench is launched."""
        expected_runtime_dir = self._runtime_workspace_path(request.request_id)
        if runtime_dir.resolve() != expected_runtime_dir.resolve():
            raise ValueError(
                "bench session must use the authoritative runtime workspace: "
                f"{expected_runtime_dir}"
            )
        return BenchSession(
            measurement_id=request.request_id,
            runtime_dir=runtime_dir,
            manifest=request.firmware,
            request=request,
            max_console_log_bytes=self.config.max_console_log_bytes,
            max_console_event_bytes=self.config.max_console_event_bytes,
            execution_timeout_s=self.config.execution_timeout_s,
            cancel_event=self.cancellation_events.setdefault(
                request.request_id,
                threading.Event(),
            ),
        )

    def publish_results(self, result: MeasurementResult) -> MeasurementResult:
        """Persist metadata and expose final artifacts to remote clients."""
        tracked_result = self.measurements.get(result.measurement_id)
        if result.identity is None and tracked_result is not None:
            result.identity = tracked_result.identity
        published_dir = self.config.artifacts_root / "measurements" / result.measurement_id
        published_dir.mkdir(parents=True, exist_ok=True)

        published_artifacts = []
        for artifact in result.artifacts:
            if artifact.path is None or not artifact.path.exists():
                continue
            if not _is_publishable_final_artifact(artifact):
                continue
            artifact_name = _safe_artifact_name(artifact.name or artifact.path.name)
            destination = published_dir / artifact_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if artifact.path.resolve() != destination.resolve():
                shutil.copy2(artifact.path, destination)
            artifact.path = destination
            artifact.name = artifact_name
            artifact.url = (
                f"/measurement/{result.measurement_id}/artifacts/"
                f"{urllib.parse.quote(artifact_name, safe='/')}"
            )
            published_artifacts.append(artifact)

        result.artifacts = published_artifacts
        result_path = published_dir / "measurement_result.json"
        result_path.write_text(
            json.dumps(measurement_result_to_dict(result), indent=2) + "\n",
            encoding="utf-8",
        )
        if not any(artifact.name == result_path.name for artifact in result.artifacts):
            result_artifact_name = (Path("cmsd") / result.measurement_id / result_path.name).as_posix()
            result_artifact_path = published_dir / result_artifact_name
            result_artifact_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(result_path, result_artifact_path)
            result.artifacts.append(
                Artifacts(
                    name=result_artifact_name,
                    path=result_artifact_path,
                    url=(
                        f"/measurement/{result.measurement_id}/artifacts/"
                        f"{urllib.parse.quote(result_artifact_name, safe='/')}"
                    ),
                    result_type=ResultType.CAMPAIGN,
                )
            )
        self.measurements[result.measurement_id] = result
        self.last_measurement_id = result.measurement_id
        if self.active_measurement_id == result.measurement_id:
            self.active_measurement_id = None
        self.cancellation_events.pop(result.measurement_id, None)
        self.cancelled_measurements.discard(result.measurement_id)
        return result

    def measurement_abort(self, measurement_id: str) -> MeasurementResult:
        """Signal the active worker; it publishes cancelled only after cleanup."""
        result = self.measurements.get(measurement_id)
        if result is None:
            return MeasurementResult(
                measurement_id=measurement_id,
                state=MeasurementState.FAILED,
                finished_at=datetime.utcnow(),
                progress_percent=100,
                current_step=MeasurementState.FAILED.value,
                message="measurement id is unknown",
            )
        cancel_event = self.cancellation_events.get(measurement_id)
        if self.active_measurement_id != measurement_id or cancel_event is None:
            return result
        cancel_event.set()
        result.message = "cancellation requested"
        self.measurements[measurement_id] = result
        self.cancelled_measurements.add(measurement_id)
        return result

    def execute_measurement(
        self,
        request: MeasurementRequest,
        staged_runtime_dir: Path | None = None,
        differential_staging: StagedDifferentialFirmware | None = None,
        identity_assigned: bool = False,
    ) -> MeasurementResult:
        """Run one V1 synchronous measurement path."""

        if differential_staging is not None:
            if request.mode is not MeasurementMode.DIFF:
                raise ValueError("differential staging requires diff measurement mode")
            if request.differential_firmware is None:
                raise ValueError("differential staging requires parent and child firmware")
            if staged_runtime_dir != differential_staging.runtime_dir:
                raise ValueError("differential staging runtime workspace does not match selection")

        request.upload_results_to_database = _should_upload_results_to_database(request.mode)

        if not identity_assigned:
            requested_run_name = request.request_id
            request.identity = None
            request.request_id = ""
            self._assign_execution_identity(request, requested_run_name)
        else:
            self._assign_execution_identity(request)
        self.measurement_accept(request)
        runtime_dir = self.create_runtime_workspace(request.request_id)
        if differential_staging is not None:
            return self._execute_differential_measurement(
                request,
                runtime_dir,
                differential_staging,
            )
        try:
            self._update_measurement(
                request.request_id,
                MeasurementState.WAITING_FOR_ARTIFACTS,
                15,
                "staging firmware artifacts",
            )
            self._raise_if_cancelled(request.request_id)
            if staged_runtime_dir is not None:
                _copy_staged_firmware(request, staged_runtime_dir, runtime_dir)
            else:
                self.fw_fetch(request, runtime_dir)
            self._update_measurement(
                request.request_id,
                MeasurementState.VALIDATING_FIRMWARE,
                35,
                "validating firmware artifacts",
            )
            self._raise_if_cancelled(request.request_id)
            session = self.build_session(request, runtime_dir)
            if self.bench is None:
                self.bench = CMSBench(config_path=self.config.bench_config)
                self.bench.initialize()
            if not self.bench.fw_is_valid(session):
                raise RuntimeError(_validation_failure_message(session))
            self._raise_if_cancelled(request.request_id)
            self._update_measurement(
                request.request_id,
                MeasurementState.SMOKE_TEST,
                50,
                "running smoke test",
            )
            self.bench.test_smoke(session)
            self._raise_if_cancelled(request.request_id)
            self._update_measurement(
                request.request_id,
                MeasurementState.RUNNING,
                70,
                "running measurement",
            )
            result = self.bench.measurement_run(session)
            if request.request_id in self.cancelled_measurements:
                result.state = MeasurementState.CANCELLED
                result.finished_at = datetime.utcnow()
                result.progress_percent = 100
                result.current_step = MeasurementState.CANCELLED.value
                result.message = "measurement cancelled"
        except (OSError, RuntimeError, ValueError) as exc:
            state = MeasurementState.CANCELLED if request.request_id in self.cancelled_measurements else MeasurementState.FAILED
            result = MeasurementResult(
                measurement_id=request.request_id,
                state=state,
                started_at=self.measurements[request.request_id].started_at,
                finished_at=datetime.utcnow(),
                progress_percent=100,
                current_step=state.value,
                message=str(exc),
                identity=request.identity,
            )
        return self.publish_results(result)

    def _execute_differential_measurement(
        self,
        request: MeasurementRequest,
        runtime_dir: Path,
        staging: StagedDifferentialFirmware,
    ) -> MeasurementResult:
        """Run parent then child through the normal Bench runner with isolated evidence."""

        started_at = self.measurements[request.request_id].started_at
        artifacts: list[Artifacts] = []
        pass_results: list[MeasurementResult] = []
        try:
            if self.bench is None:
                self.bench = CMSBench(config_path=self.config.bench_config)
                self.bench.initialize()
            self.bench.validate_differential_firmware(staging.request, staging.runtime_dir)
            self._update_measurement(request.request_id, MeasurementState.RUNNING, 50, "running parent differential pass")
            for index, variant in enumerate(("parent", "child"), start=1):
                self._raise_if_cancelled(request.request_id)
                self._update_differential_status(
                    request.request_id,
                    current_variant=variant,
                    completed_variants=index - 1,
                )
                campaign = DifferentialCampaignSession(
                    measurement_id=request.request_id,
                    staged_runtime_dir=staging.runtime_dir,
                    execution_runtime_dir=runtime_dir,
                )
                session = self.bench.activate_differential_variant(
                    request,
                    campaign,
                    variant,
                    cancel_event=self.cancellation_events[request.request_id],
                )
                pass_result = self.bench.measurement_run(session, active_root=campaign.active_root)
                pass_results.append(pass_result)
                for artifact in pass_result.artifacts:
                    artifact.name = (Path("results") / variant / artifact.name).as_posix()
                    artifacts.append(artifact)
                if index == 1:
                    self._update_measurement(request.request_id, MeasurementState.RUNNING, 75, "running child differential pass")
                    self._update_differential_status(
                        request.request_id,
                        current_variant="child",
                        completed_variants=1,
                        passed_count=sum(item.state is MeasurementState.PASSED for item in pass_results),
                        failed_count=sum(item.state is not MeasurementState.PASSED for item in pass_results),
                    )
            campaign_path = self.bench.record_differential_campaign_order(runtime_dir)
            artifacts.append(Artifacts(name="results/differential_campaign.json", path=campaign_path, result_type=ResultType.DIAGNOSTIC))
            state = MeasurementState.PASSED if all(item.state is MeasurementState.PASSED for item in pass_results) else MeasurementState.FAILED
            result = MeasurementResult(
                measurement_id=request.request_id,
                state=state,
                started_at=started_at,
                finished_at=datetime.utcnow(),
                progress_percent=100,
                current_step=state.value,
                message="differential parent and child passes completed",
                identity=request.identity,
                artifacts=artifacts,
                mode=MeasurementMode.DIFF.value,
                completed_variants=2,
                total_variants=2,
                passed_count=sum(item.state is MeasurementState.PASSED for item in pass_results),
                failed_count=sum(item.state is not MeasurementState.PASSED for item in pass_results),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            result = MeasurementResult(
                measurement_id=request.request_id,
                state=MeasurementState.FAILED,
                started_at=started_at,
                finished_at=datetime.utcnow(),
                progress_percent=100,
                current_step=MeasurementState.FAILED.value,
                message=str(exc),
                identity=request.identity,
                artifacts=artifacts,
            )
        return self.publish_results(result)

    def create_app(self) -> Any:
        """Create the FastAPI app for daemon bringup routes."""

        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse

        app = FastAPI(title="CMS Daemon")

        def require_observation_protocol(protocol_version: int) -> None:
            if protocol_version != CMS_OBSERVATION_PROTOCOL_VERSION:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "incompatible_protocol",
                        "message": "observation protocol version is not supported",
                        "received_version": protocol_version,
                        "supported_version": CMS_OBSERVATION_PROTOCOL_VERSION,
                    },
                )

        def raise_observation_error(exc: Exception) -> None:
            if isinstance(exc, KeyError):
                raise HTTPException(
                    status_code=404,
                    detail={"code": "unknown_measurement", "message": str(exc.args[0])},
                ) from exc
            if isinstance(exc, OSError):
                raise HTTPException(
                    status_code=503,
                    detail={"code": "observation_unavailable", "message": str(exc)},
                ) from exc
            raise HTTPException(
                status_code=500,
                detail={"code": "observation_data_invalid", "message": str(exc)},
            ) from exc

        @app.get("/health")
        def health() -> dict[str, Any]:
            return bench_health_report_to_dict(self.get_health())

        @app.get("/bench")
        def daemon_status() -> dict[str, Any]:
            return self.get_status()

        @app.get("/tests")
        def available_tests() -> dict[str, Any]:
            features = []
            config_root = Path("tests") / "configs" / "ble"
            if config_root.exists():
                for feature_dir in sorted(path for path in config_root.iterdir() if path.is_dir()):
                    configs = sorted(path.stem for path in feature_dir.glob("*.json"))
                    features.append({"feature": feature_dir.name, "configs": configs})
            return {
                "modes": ["debug", "default", "full_sweep", "diff"],
                "suite": "ble",
                "features": features,
            }

        @app.get("/result_artifacts")
        def artifacts() -> dict[str, str]:
            return self.files.as_dict()

        @app.post("/measurement")
        def request_measurement(request: dict[str, Any]) -> dict[str, Any]:
            result = self.execute_measurement(measurement_request_from_dict(request))
            return measurement_result_to_dict(result)

        @app.post("/firmware")
        def send_firmware(request: dict[str, Any]) -> dict[str, Any]:
            result = self.stage_firmware(
                measurement_request_from_dict(request),
                image_payloads=request.get("image_payloads", {}),
            )
            return measurement_result_to_dict(result)

        @app.post("/test")
        def run_test(request: dict[str, Any]) -> dict[str, Any]:
            result = self.start_staged_test(
                mode=str(request.get("mode", "default")),
                feature=str(request.get("feature", "")),
                config=str(request.get("config", "default")),
                config_hash=str(request.get("config_hash", "")),
                variable_filters=variable_filters_from_dicts(request.get("variable_filters")),
                run_name=str(request.get("run_name", "")),
                upload_results_to_database=bool(request.get("upload_results_to_database", True)),
                debug_artifact_streaming=bool(request.get("debug_artifact_streaming", False)),
                staging_id=str(request.get("staging_id", "")),
            )
            return measurement_result_to_dict(result)

        @app.get("/measurement/active")
        def active_measurement(
            protocol_version: int = CMS_OBSERVATION_PROTOCOL_VERSION,
        ) -> dict[str, Any]:
            require_observation_protocol(protocol_version)
            return {
                "protocol_version": CMS_OBSERVATION_PROTOCOL_VERSION,
                "measurement_id": self.get_active_measurement_id(),
            }

        @app.get("/measurements/completed")
        def completed_measurements(
            protocol_version: int = CMS_OBSERVATION_PROTOCOL_VERSION,
        ) -> dict[str, Any]:
            require_observation_protocol(protocol_version)
            return {
                "protocol_version": CMS_OBSERVATION_PROTOCOL_VERSION,
                "retention_limit": self.config.completed_measurement_retention,
                "measurements": [
                    measurement_result_to_dict(result)
                    for result in self.completed_measurements()
                ],
            }

        @app.get("/measurement/{measurement_id}/summary")
        def measurement_summary(
            measurement_id: str,
            protocol_version: int = CMS_OBSERVATION_PROTOCOL_VERSION,
        ) -> dict[str, Any]:
            require_observation_protocol(protocol_version)
            try:
                return measurement_summary_to_dict(self.get_measurement_summary(measurement_id))
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise_observation_error(exc)

        @app.get("/measurement/{measurement_id}/events")
        def measurement_events(
            measurement_id: str,
            after: str = "0",
            protocol_version: int = CMS_OBSERVATION_PROTOCOL_VERSION,
        ) -> dict[str, Any]:
            require_observation_protocol(protocol_version)
            try:
                after_cursor = EventCursor(int(after))
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_cursor", "message": str(exc)},
                ) from exc
            try:
                return measurement_event_page_to_dict(
                    self.get_measurement_events(measurement_id, after_cursor)
                )
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise_observation_error(exc)

        @app.get("/measurement/{measurement_id}")
        def measurement_status(measurement_id: str) -> dict[str, Any]:
            return measurement_result_to_dict(self.get_measurement_result(measurement_id))

        @app.get("/measurement/{measurement_id}/artifacts")
        def measurement_artifacts(
            measurement_id: str,
            protocol_version: int = CMS_OBSERVATION_PROTOCOL_VERSION,
        ) -> dict[str, Any]:
            require_observation_protocol(protocol_version)
            try:
                self._runtime_workspace_path(measurement_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_measurement_id", "message": str(exc)},
                ) from exc
            result = self.measurements.get(measurement_id)
            if result is None:
                result_path = self.config.artifacts_root / "measurements" / measurement_id / "measurement_result.json"
                if result_path.exists():
                    try:
                        payload = json.loads(result_path.read_text(encoding="utf-8"))
                    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise_observation_error(exc)
                    return {
                        "protocol_version": CMS_OBSERVATION_PROTOCOL_VERSION,
                        "measurement_id": measurement_id,
                        "artifacts": payload.get("artifacts", []),
                    }
                raise_observation_error(
                    KeyError(f"measurement id is unknown: {measurement_id}")
                )
            return {
                "protocol_version": CMS_OBSERVATION_PROTOCOL_VERSION,
                "measurement_id": measurement_id,
                "artifacts": measurement_result_to_dict(result)["artifacts"],
            }

        @app.get("/artifacts/last")
        def last_measurement_artifacts() -> dict[str, Any]:
            if self.last_measurement_id is None:
                return {"measurement_id": "", "artifacts": []}
            result = self.measurements.get(self.last_measurement_id)
            if result is None:
                return {"measurement_id": self.last_measurement_id, "artifacts": []}
            return {
                "measurement_id": self.last_measurement_id,
                "artifacts": measurement_result_to_dict(result)["artifacts"],
            }

        @app.get("/measurement/{measurement_id}/artifacts/{artifact_path:path}")
        def measurement_artifact_file(measurement_id: str, artifact_path: str) -> FileResponse:
            try:
                self._runtime_workspace_path(measurement_id)
                safe_name = _safe_artifact_name(urllib.parse.unquote(artifact_path))
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_artifact_path", "message": str(exc)},
                ) from exc
            published_dir = self.config.artifacts_root / "measurements" / measurement_id
            if not published_dir.is_dir():
                raise_observation_error(
                    KeyError(f"measurement id is unknown: {measurement_id}")
                )
            requested_artifact = published_dir / safe_name
            if not requested_artifact.is_file():
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "unknown_artifact",
                        "message": f"artifact is unavailable: {safe_name}",
                    },
                )
            return FileResponse(requested_artifact)

        @app.post("/measurement/{measurement_id}/cancel")
        def cancel_measurement(measurement_id: str) -> dict[str, Any]:
            return measurement_result_to_dict(self.measurement_abort(measurement_id))

        @app.post("/cancel")
        def cancel_active_measurement() -> dict[str, Any]:
            measurement_id = self.active_measurement_id or self.last_measurement_id or ""
            return measurement_result_to_dict(self.measurement_abort(measurement_id))

        return app

    def start_service(self) -> None:
        """Start the HTTP API that exposes daemon bringup state."""

        import uvicorn

        self.initialize()
        uvicorn.run(self.create_app(), host=self.config.host, port=self.config.port)

    def _bench_health(self) -> BenchHealthReport:
        if self.bench is None:
            self.bench = CMSBench(config_path=self.config.bench_config)
            self.bench.initialize()
        return self.bench.health_check()

    def _daemon_checks(self) -> list[BenchCheckResult]:
        return [
            _path_writable_check("runtime_storage", self.config.runtime_root),
            _runtime_filesystem_check(
                self.config.runtime_root,
                self.config.expected_runtime_filesystems,
            ),
            _path_writable_check("artifact_storage", self.config.artifacts_root),
            _file_check("bench_config", self.config.bench_config),
        ]

    def _update_measurement(
        self,
        measurement_id: str,
        state: MeasurementState,
        progress_percent: int,
        message: str,
    ) -> None:
        result = self.measurements[measurement_id]
        result.state = state
        result.progress_percent = progress_percent
        result.current_step = state.value
        result.message = message

    def _update_differential_status(
        self,
        measurement_id: str,
        *,
        current_variant: str,
        completed_variants: int,
        current_test: str = "",
        passed_count: int = 0,
        failed_count: int = 0,
    ) -> None:
        """Publish paired-campaign truth through the main measurement stream."""

        result = self.measurements[measurement_id]
        result.mode = MeasurementMode.DIFF.value
        result.current_variant = current_variant
        result.completed_variants = completed_variants
        result.total_variants = 2
        result.current_test = current_test
        result.passed_count = passed_count
        result.failed_count = failed_count
        runtime_dir = self._runtime_workspace_path(measurement_id)
        stream = MeasurementEventStream(
            measurement_id,
            MeasurementRuntimePaths.from_root(runtime_dir),
            max_console_bytes=self.config.max_console_event_bytes,
        )
        payload = {
            "mode": result.mode,
            "current_variant": current_variant,
            "completed_variants": completed_variants,
            "total_variants": 2,
            "current_test": current_test,
            "passed_count": passed_count,
            "failed_count": failed_count,
        }
        stream.replace_status({
            "state": result.state.value,
            "progress_percent": result.progress_percent,
            "current_step": result.current_step,
            "message": result.message,
            **payload,
        })
        stream.append_lifecycle("differential_status", **payload)

    def _assign_execution_identity(
        self,
        request: MeasurementRequest,
        requested_run_name: str = "",
    ):
        """Attach the daemon-authoritative identity for one test execution."""
        if request.identity is not None:
            request.request_id = request.identity.measurement_id
            return request.identity

        run_name = requested_run_name.strip() or request.request_id.strip()
        if not run_name:
            selector = request.selector
            parts = [selector.feature, selector.config, request.mode.value]
            run_name = "-".join(part for part in parts if part) or "measurement"
        request.identity = create_measurement_identity(run_name)
        request.request_id = request.identity.measurement_id
        return request.identity

    def _raise_if_cancelled(self, measurement_id: str) -> None:
        if measurement_id in self.cancelled_measurements:
            raise RuntimeError("measurement cancelled")

    def _daemon_files(self) -> DaemonFiles:
        return DaemonFiles(
            result_json=self.config.artifacts_root / "result.json",
            result_npz=self.config.artifacts_root / "result.npz",
            dut_image=self.config.runtime_root / "dut.s37",
            dut_image_hash=self.config.runtime_root / "dut.s37.sha256",
            tester_image=self.config.runtime_root / "tester.s37",
            tester_image_hash=self.config.runtime_root / "tester.s37.sha256",
            measurement_request=self.config.runtime_root / "measurement_request.json",
        )

    def _runtime_workspace_path(self, measurement_id: str) -> Path:
        # The measurement ID becomes one directory name below runtime_root.
        # Path.name handles platform-specific separators; comparing it with the
        # original value rejects absolute or multi-component paths, while the
        # explicit dot names prevent traversal outside the measurement root.
        if (
            not measurement_id
            or measurement_id in {".", ".."}
            or Path(measurement_id).name != measurement_id
        ):
            raise ValueError("measurement_id must be a single non-empty path component")
        return self.config.runtime_root / "measurements" / measurement_id

    def _observation_runtime_path(self, measurement_id: str) -> Path:
        runtime_dir = self._runtime_workspace_path(measurement_id)
        if measurement_id not in self.measurements and not runtime_dir.is_dir():
            raise KeyError(f"measurement id is unknown: {measurement_id}")
        return runtime_dir

    def _published_measurement_path(self, measurement_id: str) -> Path:
        self._runtime_workspace_path(measurement_id)
        return self.config.artifacts_root / "measurements" / measurement_id

    def _observation_run_file_paths(self, measurement_id: str) -> tuple[Path, Path]:
        runtime_dir = self._runtime_workspace_path(measurement_id)
        if runtime_dir.is_dir():
            return (
                runtime_dir / "orchestrator_plan.json",
                runtime_dir / "orchestrator_results.jsonl",
            )
        published_dir = self._published_measurement_path(measurement_id)
        if published_dir.is_dir():
            return (
                published_dir / "orchestrator_plan.json",
                published_dir / "orchestrator_results.jsonl",
            )
        if measurement_id in self.measurements:
            return (
                runtime_dir / "orchestrator_plan.json",
                runtime_dir / "orchestrator_results.jsonl",
            )
        raise KeyError(f"measurement id is unknown: {measurement_id}")

    def _observation_measurement_result(self, measurement_id: str) -> MeasurementResult | None:
        result = self.measurements.get(measurement_id)
        if result is not None:
            return result
        result_path = self._published_measurement_path(measurement_id) / "measurement_result.json"
        if result_path.exists():
            return measurement_result_from_dict(json.loads(result_path.read_text(encoding="utf-8")))
        return None

    def _touch_staging_files(self) -> None:
        for path in self.files.as_dict().values():
            file_path = Path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.touch(exist_ok=True)


def _path_writable_check(name: str, path: Path) -> BenchCheckResult:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".cms_write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return BenchCheckResult(
            name=name,
            status=BenchCheckStatus.FAIL,
            summary=f"{name} is not writable",
            detail=str(exc),
        )
    return BenchCheckResult(
        name=name,
        status=BenchCheckStatus.PASS,
        summary=f"{name} is writable",
        detail=str(path),
    )


def _runtime_filesystem_check(
    path: Path,
    expected_filesystems: tuple[str, ...],
) -> BenchCheckResult:
    """Verify RAM backing on Linux and report runtime storage capacity."""

    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return BenchCheckResult(
            name="runtime_filesystem",
            status=BenchCheckStatus.FAIL,
            summary="runtime filesystem capacity is unavailable",
            detail=f"path={path}; error={exc}",
        )

    system_name = platform.system()
    capacity_detail = (
        f"path={path}; available_bytes={usage.free}; total_bytes={usage.total}"
    )
    if system_name != "Linux":
        return BenchCheckResult(
            name="runtime_filesystem",
            status=BenchCheckStatus.WARN,
            summary=f"runtime filesystem verification is unsupported on {system_name}",
            detail=capacity_detail,
        )

    try:
        filesystem_type = _linux_filesystem_type(path)
    except OSError as exc:
        return BenchCheckResult(
            name="runtime_filesystem",
            status=BenchCheckStatus.FAIL,
            summary="runtime filesystem type could not be determined",
            detail=f"{capacity_detail}; error={exc}",
        )

    expected = tuple(item.lower() for item in expected_filesystems)
    expected_detail = ",".join(expected)
    detail = (
        f"{capacity_detail}; filesystem={filesystem_type or 'unknown'}; "
        f"expected={expected_detail}"
    )
    if filesystem_type is None:
        return BenchCheckResult(
            name="runtime_filesystem",
            status=BenchCheckStatus.FAIL,
            summary="runtime filesystem type could not be determined",
            detail=detail,
        )
    if filesystem_type.lower() not in expected:
        return BenchCheckResult(
            name="runtime_filesystem",
            status=BenchCheckStatus.FAIL,
            summary=f"runtime filesystem {filesystem_type} is not RAM-backed",
            detail=detail,
        )
    return BenchCheckResult(
        name="runtime_filesystem",
        status=BenchCheckStatus.PASS,
        summary=f"runtime filesystem is {filesystem_type}",
        detail=detail,
    )


def _linux_filesystem_type(path: Path) -> str | None:
    mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    return _filesystem_type_from_mountinfo(path.resolve().as_posix(), mountinfo)


def _filesystem_type_from_mountinfo(path: str, mountinfo: str) -> str | None:
    """Return the filesystem type of the most specific Linux mount for path."""

    resolved_path = PurePosixPath(path)
    best_match: tuple[int, str] | None = None
    for line in mountinfo.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = PurePosixPath(_decode_mountinfo_path(fields[4]))
            filesystem_type = fields[separator + 1]
            resolved_path.relative_to(mount_point)
        except (IndexError, ValueError):
            continue
        specificity = len(mount_point.parts)
        if best_match is None or specificity > best_match[0]:
            best_match = (specificity, filesystem_type)
    return best_match[1] if best_match is not None else None


def _decode_mountinfo_path(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _measurement_result_sort_key(result: MeasurementResult) -> datetime:
    timestamp = result.finished_at or result.started_at
    if timestamp is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def _measurement_result_from_runtime_status(
    measurement_id: str,
    status: dict[str, Any],
    *,
    fallback: MeasurementResult | None = None,
) -> MeasurementResult:
    state_text = str(status.get("state", ""))
    try:
        state = MeasurementState(state_text)
    except ValueError:
        state = MeasurementState.RUNNING
    progress = fallback.progress_percent if fallback is not None else 0
    if "progress_percent" in status:
        try:
            progress = int(status["progress_percent"])
        except (TypeError, ValueError):
            progress = fallback.progress_percent if fallback is not None else 0
    current_step = str(status.get("current_step", status.get("state", state.value)))
    message = str(status.get("message", ""))
    if not message and "pid" in status:
        message = f"active process pid={status['pid']}"
    return MeasurementResult(
        measurement_id=measurement_id,
        state=state,
        started_at=fallback.started_at if fallback is not None else None,
        finished_at=fallback.finished_at if fallback is not None else None,
        artifacts=list(fallback.artifacts) if fallback is not None else [],
        progress_percent=progress,
        current_step=current_step,
        message=message,
        identity=fallback.identity if fallback is not None else None,
    )


def _interrupted_result_from_runtime_status(
    measurement_id: str,
    status: dict[str, Any],
) -> MeasurementResult:
    progress = 100
    if "progress_percent" in status:
        try:
            progress = max(0, min(100, int(status["progress_percent"])))
        except (TypeError, ValueError):
            progress = 100
    previous_state = str(status.get("state", "")).strip()
    previous_step = str(status.get("current_step", previous_state)).strip()
    detail = previous_step or previous_state
    message = "daemon restarted before measurement completed; process was not reattached"
    if detail:
        message = f"{message} (previous step: {detail})"
    return MeasurementResult(
        measurement_id=measurement_id,
        state=MeasurementState.INTERRUPTED,
        finished_at=datetime.utcnow(),
        progress_percent=progress,
        current_step=MeasurementState.INTERRUPTED.value,
        message=message,
    )


def _runtime_workspace_has_run_evidence(runtime_dir: Path) -> bool:
    evidence_names = (
        "current_status.json",
        "events.jsonl",
        "orchestrator_plan.json",
        "orchestrator_results.jsonl",
        "test_stdout.log",
        "test_stderr.log",
    )
    return any((runtime_dir / name).exists() for name in evidence_names)


def _read_optional_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _published_event_records_from_results(
    measurement_id: str,
    results_path: Path,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for cursor, result_record in enumerate(_read_jsonl_records(results_path), start=1):
        record_type = str(result_record.get("record_type", "job_result"))
        timestamp = (
            result_record.get("timestamp")
            or result_record.get("finished_at")
            or result_record.get("started_at")
            or datetime.now(timezone.utc).isoformat()
        )
        records.append(
            {
                **result_record,
                "measurement_id": measurement_id,
                "cursor": cursor,
                "timestamp": timestamp,
                "type": "lifecycle",
                "state": record_type,
            }
        )
    return records


def _file_check(name: str, path: Path) -> BenchCheckResult:
    if path.exists():
        return BenchCheckResult(
            name=name,
            status=BenchCheckStatus.PASS,
            summary=f"{name} exists",
            detail=str(path),
        )
    return BenchCheckResult(
        name=name,
        status=BenchCheckStatus.FAIL,
        summary=f"{name} is missing",
        detail=str(path),
    )


def _stage_firmware_image(source: str, destination: Path) -> None:
    if not source:
        raise RuntimeError(f"firmware source is missing for {destination.name}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in ("http", "https"):
        urllib.request.urlretrieve(source, destination)
        return
    if parsed.scheme == "file":
        source_path = Path(urllib.request.url2pathname(parsed.path))
    else:
        source_path = Path(source)
    if not source_path.exists():
        raise RuntimeError(f"firmware source does not exist: {source}")
    shutil.copy2(source_path, destination)


def _safe_artifact_name(name: str) -> str:
    normalized = Path(name.replace("\\", "/"))
    if normalized.is_absolute() or any(part in {"", ".", ".."} for part in normalized.parts):
        raise RuntimeError(f"invalid artifact name: {name}")
    return normalized.as_posix()


def _is_publishable_final_artifact(artifact: Artifacts) -> bool:
    """Allow only selected completed-run evidence onto persistent storage."""

    artifact_name = Path((artifact.name or "").replace("\\", "/")).name
    if artifact.result_type is ResultType.CURRENT_MEASUREMENT:
        return True
    if artifact.result_type is ResultType.PYTEST_JUNIT:
        return artifact_name.lower().endswith(".xml")
    if artifact.result_type is ResultType.DIAGNOSTIC:
        return artifact_name in PUBLISHABLE_DIAGNOSTIC_NAMES
    return False


def _staged_test_failure(message: str) -> MeasurementResult:
    """Return a rejected staged-test request before any bench work begins."""

    return MeasurementResult(
        measurement_id="",
        state=MeasurementState.FAILED,
        finished_at=datetime.utcnow(),
        progress_percent=100,
        current_step=MeasurementState.FAILED.value,
        message=message,
    )


def _copy_staged_firmware(
    request: MeasurementRequest,
    staged_runtime_dir: Path,
    runtime_dir: Path,
) -> None:
    images = [
        request.firmware.dut_img,
        request.firmware.tester_img,
        *request.firmware.dut_variants.values(),
    ]
    if request.firmware.xapi_file is not None:
        images.append(request.firmware.xapi_file)
    if request.firmware.bootloader_img is not None:
        images.append(request.firmware.bootloader_img)

    runtime_dir.mkdir(parents=True, exist_ok=True)
    for image in images:
        source = staged_runtime_dir / image.name
        if not source.exists():
            raise RuntimeError(f"staged firmware image is missing: {source}")
        shutil.copy2(source, runtime_dir / image.name)


def _write_firmware_payloads(
    request: MeasurementRequest,
    runtime_dir: Path,
    image_payloads: dict[str, str],
) -> None:
    targets = {
        "dut_img": request.firmware.dut_img,
        "tester_img": request.firmware.tester_img,
    }
    targets.update(
        {
            f"dut_variant:{name}": image
            for name, image in request.firmware.dut_variants.items()
        }
    )
    if request.firmware.xapi_file is not None:
        targets["xapi_file"] = request.firmware.xapi_file
    if request.firmware.bootloader_img is not None:
        targets["bootloader_img"] = request.firmware.bootloader_img

    for key, image in targets.items():
        payload = image_payloads.get(key)
        if not payload:
            raise RuntimeError(f"firmware payload is missing for {key}")
        destination = runtime_dir / image.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(base64.b64decode(payload))


def _write_differential_firmware_payloads(
    request: MeasurementRequest,
    runtime_dir: Path,
    image_payloads: dict[str, str],
) -> None:
    """Publish a complete parent/child payload pair only after every asset is present."""

    differential = request.differential_firmware
    if differential is None:
        raise RuntimeError("differential firmware payloads require a differential firmware request")
    destination_root = runtime_dir / "firmware"
    if destination_root.exists():
        raise RuntimeError(f"differential firmware staging destination already exists: {destination_root}")
    _validate_differential_asset_names(differential)

    temporary_root = Path(tempfile.mkdtemp(prefix=".firmware-staging-", dir=runtime_dir))
    try:
        staged_root = temporary_root / "firmware"
        for variant_name, firmware in (("child", differential.child), ("parent", differential.parent)):
            for role_image in firmware.role_images:
                role = role_image.role
                project_name = role_image.project_name
                if Path(role).name != role or role in {".", ".."}:
                    raise RuntimeError(f"invalid differential {variant_name} role: {role}")
                if Path(project_name).name != project_name or project_name in {".", ".."}:
                    raise RuntimeError(f"invalid differential {variant_name} project name: {project_name}")
                key = f"differential_firmware.{variant_name}.role_images.{project_name}"
                _write_differential_payload(
                    image_payloads,
                    key,
                    staged_root / variant_name / "roles" / role / project_name / role_image.image.name,
                )
            if firmware.xapi_file is None:
                raise RuntimeError(f"differential {variant_name}.xapi_file is required")
            _write_differential_payload(
                image_payloads,
                f"differential_firmware.{variant_name}.xapi_file",
                staged_root / variant_name / "xapi" / firmware.xapi_file.name,
            )
            if firmware.bootloader_img is not None:
                _write_differential_payload(
                    image_payloads,
                    f"differential_firmware.{variant_name}.bootloader_img",
                    staged_root / variant_name / "bootloader" / firmware.bootloader_img.name,
                )
        (staged_root / "manifest.json").write_text(
            json.dumps(
                {"request_id": request.request_id, "variants": differential_firmware_to_dict(differential)},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        staged_root.replace(destination_root)
    except Exception:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise
    finally:
        if temporary_root.exists():
            shutil.rmtree(temporary_root, ignore_errors=True)


def _write_differential_payload(
    image_payloads: dict[str, str],
    key: str,
    destination: Path,
) -> None:
    payload = image_payloads.get(key)
    if not payload:
        raise RuntimeError(f"firmware payload is missing for {key}")
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"firmware payload is invalid for {key}") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(decoded)


def _validate_differential_asset_names(differential: Any) -> None:
    """Keep each variant's staged filenames unambiguous before any files are written."""

    for variant_name, firmware in (("child", differential.child), ("parent", differential.parent)):
        images = [role_image.image for role_image in firmware.role_images]
        if firmware.xapi_file is not None:
            images.append(firmware.xapi_file)
        if firmware.bootloader_img is not None:
            images.append(firmware.bootloader_img)
        names = [image.name for image in images]
        if any(Path(name).name != name or name in {"", ".", ".."} for name in names):
            raise RuntimeError(f"differential {variant_name} firmware contains an invalid asset name")
        if len(names) != len(set(names)):
            raise RuntimeError(f"differential {variant_name} firmware contains duplicate asset names")


def _validation_failure_message(session: BenchSession) -> str:
    if session.notes:
        return session.notes[-1]
    return "firmware validation failed"


if __name__ == "__main__":
    cmsd = CMSDaemon()
    cmsd.start_service()

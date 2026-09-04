"""Bench-local execution primitives for the Current Measurement System.

`cms_bench.py` is intentionally transport-agnostic. It does not expose HTTP
routes or own remote client policy. Its responsibility is to control the local
bench hardware and execute one measurement run at a time when asked by
`cmsd.py`.
"""

from __future__ import annotations

import json
import os
import platform
import signal
import shutil
import hashlib
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from cms_protocol import (
    BenchCheckResult,
    BenchCheckStatus,
    BenchHealthReport,
    BenchIdentity,
    BenchState,
    DeviceState,
    EventCursor,
    Artifacts,
    FirmwareData,
    MeasurementRequest,
    MeasurementResult,
    MeasurementState,
    ResultType,
    measurement_request_to_dict,
    measurement_result_to_dict,
    measurement_identity_to_dict,
    variable_filters_to_dicts,
)

CMS_BENCH_CONFIG = "cms_bench_runtime.json"
DEFAULT_CONSOLE_LOG_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_CONSOLE_EVENT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_EXECUTION_TIMEOUT_S = 5 * 60
PROCESS_TERMINATE_GRACE_S = 5.0
MAX_CONSOLE_EVENT_SEGMENT_BYTES = 64 * 1024


@dataclass(slots=True)
class BenchResources:
    """Static hardware mapping needed to operate one physical bench."""

    dut_probe_sn: str = ""
    tester_probe_sn: str = ""
    dut_serial_port: str = ""
    tester_serial_port: str = ""
    joulescope_serial: str = ""
    joulescope_wake_gpio: int | None = None
    board: str = ""
    board_rev: str = ""
    device: str = ""
    part_number: str = ""
    serial_number: str = ""
    result_db: str = ""
    commander_path: str = ""
    xapi_path: str = ""
    group_id: int | None = None
    group_name: str = ""


@dataclass(frozen=True, slots=True)
class MeasurementRuntimePaths:
    """Standard active files owned by one measurement runtime workspace."""

    root: Path
    status: Path
    events: Path
    console_events: Path
    stdout: Path
    stderr: Path

    @classmethod
    def from_root(cls, root: Path) -> "MeasurementRuntimePaths":
        return cls(
            root=root,
            status=root / "current_status.json",
            events=root / "events.jsonl",
            console_events=root / "console_events",
            stdout=root / "test_stdout.log",
            stderr=root / "test_stderr.log",
        )

    def active_paths(self) -> tuple[Path, ...]:
        return (self.status, self.events, self.console_events, self.stdout, self.stderr)


class MeasurementEventStream:
    """Thread-safe append-only events and atomic status for one measurement."""

    def __init__(
        self,
        measurement_id: str,
        paths: MeasurementRuntimePaths,
        *,
        max_console_bytes: int = DEFAULT_CONSOLE_EVENT_MAX_BYTES,
    ):
        if max_console_bytes < 512:
            raise ValueError("max_console_bytes must be at least 512")
        self.measurement_id = measurement_id
        self.paths = paths
        self.max_console_bytes = max_console_bytes
        self._segment_target_bytes = min(
            MAX_CONSOLE_EVENT_SEGMENT_BYTES,
            max_console_bytes,
        )
        self._lock = threading.Lock()
        self._console_segments = _console_segment_paths(paths.console_events)
        self._console_segment_bytes = sum(path.stat().st_size for path in self._console_segments)
        self._cursor = EventCursor(_last_event_cursor(paths))
        self._oldest_retained_cursor = _first_console_cursor(self._console_segments)
        if self._oldest_retained_cursor == 0 and self._cursor.value:
            self._oldest_retained_cursor = 1
        self._truncated_log_sources = _truncated_log_sources(paths.events)

    @property
    def cursor(self) -> int:
        with self._lock:
            return self._cursor.value

    @property
    def oldest_retained_cursor(self) -> int:
        with self._lock:
            return self._oldest_retained_cursor

    def append_lifecycle(self, state: str, **details: Any) -> dict[str, Any]:
        with self._lock:
            return self._append_structured_locked(state, **details)

    def append_console(self, source: str, text: str) -> dict[str, Any]:
        if source not in {"stdout", "stderr"}:
            raise ValueError("console event source must be stdout or stderr")
        with self._lock:
            record = self._next_record_locked("console", source=source, text=text)
            payload, record = _bounded_console_payload(record, self.max_console_bytes)
            self._append_console_payload_locked(record["cursor"], payload)
            self._cursor = EventCursor(record["cursor"])
            discarded_through = self._prune_console_segments_locked()
            if discarded_through is not None:
                self._oldest_retained_cursor = _first_console_cursor(self._console_segments)
                self._append_structured_locked(
                    "console_truncated",
                    discarded_through_cursor=discarded_through,
                    oldest_retained_cursor=self._oldest_retained_cursor,
                )
            elif self._oldest_retained_cursor == 0:
                self._oldest_retained_cursor = record["cursor"]
            return record

    def record_log_truncation(self, source: str, discarded_bytes: int) -> None:
        """Record the first bounded-tail truncation for each raw output log."""

        with self._lock:
            if source in self._truncated_log_sources:
                return
            self._truncated_log_sources.add(source)
            self._append_structured_locked(
                "log_truncated",
                source=source,
                discarded_bytes=discarded_bytes,
            )

    def read_events(self) -> list[dict[str, Any]]:
        """Return retained structured and console events in cursor order."""

        with self._lock:
            records = _read_jsonl_records(self.paths.events)
            for segment in self._console_segments:
                records.extend(_read_jsonl_records(segment))
            return sorted(records, key=lambda record: int(record.get("cursor", 0)))

    def replace_status(self, status: dict[str, Any]) -> None:
        """Atomically replace the small current-status snapshot."""

        with self._lock:
            snapshot = {
                **status,
                "measurement_id": self.measurement_id,
                "cursor": self._cursor.value,
                "oldest_retained_cursor": self._oldest_retained_cursor,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self.paths.root.mkdir(parents=True, exist_ok=True)
            temporary_path = self.paths.status.with_name(self.paths.status.name + ".tmp")
            temporary_path.write_text(
                json.dumps(snapshot, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, self.paths.status)

    def _next_record_locked(self, event_type: str, **details: Any) -> dict[str, Any]:
        return {
            **details,
            "measurement_id": self.measurement_id,
            "cursor": self._cursor.next().value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
        }

    def _append_structured_locked(self, state: str, **details: Any) -> dict[str, Any]:
        record = self._next_record_locked("lifecycle", state=state, **details)
        self.paths.root.mkdir(parents=True, exist_ok=True)
        with self.paths.events.open("a", encoding="utf-8") as event_file:
            event_file.write(json.dumps(record, separators=(",", ":")) + "\n")
            event_file.flush()
        self._cursor = EventCursor(record["cursor"])
        if self._oldest_retained_cursor == 0:
            self._oldest_retained_cursor = record["cursor"]
        return record

    def _append_console_payload_locked(self, cursor: int, payload: bytes) -> None:
        self.paths.console_events.mkdir(parents=True, exist_ok=True)
        current = self._console_segments[-1] if self._console_segments else None
        if (
            current is None
            or (current.stat().st_size > 0 and current.stat().st_size + len(payload) > self._segment_target_bytes)
        ):
            current = self.paths.console_events / f"{cursor:020d}.jsonl"
            self._console_segments.append(current)
        with current.open("ab") as segment_file:
            segment_file.write(payload)
            segment_file.flush()
        self._console_segment_bytes += len(payload)

    def _prune_console_segments_locked(self) -> int | None:
        discarded_through: int | None = None
        while self._console_segment_bytes > self.max_console_bytes and len(self._console_segments) > 1:
            oldest = self._console_segments.pop(0)
            oldest_records = _read_jsonl_records(oldest)
            if oldest_records:
                discarded_through = max(
                    discarded_through or 0,
                    max(int(record["cursor"]) for record in oldest_records),
                )
            self._console_segment_bytes -= oldest.stat().st_size
            oldest.unlink()
        return discarded_through


@dataclass(slots=True)
class BenchSession:
    """Execution context prepared by the daemon for one active measurement."""

    measurement_id: str
    runtime_dir: Path
    manifest: FirmwareData
    request: MeasurementRequest
    notes: list[str] = field(default_factory=list)
    max_console_log_bytes: int = DEFAULT_CONSOLE_LOG_MAX_BYTES
    max_console_event_bytes: int = DEFAULT_CONSOLE_EVENT_MAX_BYTES
    execution_timeout_s: float = DEFAULT_EXECUTION_TIMEOUT_S
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    event_stream: MeasurementEventStream = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.event_stream = MeasurementEventStream(
            self.measurement_id,
            self.runtime_paths,
            max_console_bytes=self.max_console_event_bytes,
        )

    @property
    def runtime_paths(self) -> MeasurementRuntimePaths:
        return MeasurementRuntimePaths.from_root(self.runtime_dir)


@dataclass(slots=True)
class CMSBenchRunRequest:
    """Compatibility request object for the older local bench runner helper."""

    dut_app: str
    tester_app: str
    analysis: "CMSBenchResultAnalysis"


class CMSBenchResultAnalysis(str, Enum):
    """Compatibility enum for older bench tests."""

    DEBUG = "debug"
    DEFAULT = "default"
    FULL_SWEEP = "full_sweep"
    DIFF = "diff"


CMSBenchResources = BenchResources
CMSBenchState = BenchState


class CMSBench:
    """Own the hardware-facing steps for one current-measurement bench."""

    def __init__(
        self,
        identity: BenchIdentity | None = None,
        resources: BenchResources | None = None,
        config_path: str | Path = CMS_BENCH_CONFIG,
    ):
        self.config_path = Path(config_path)
        self.identity = identity
        self.resources = resources
        self.state = BenchState.OFFLINE
        self.initialized = False
        self.startup_error = ""

        self.dut_serial: str | None = None
        self.tester_serial: str | None = None
        self.js_interface: Any = None

    def initialize(self) -> None:
        """Load and validate persistent bench configuration."""

        try:
            if self.identity is None or self.resources is None:
                self.identity, self.resources = load_bench_runtime_config(self.config_path)

            missing = self._missing_required_config()
            if missing:
                self.initialized = False
                self.state = BenchState.ERROR
                self.startup_error = "missing required bench config: " + ", ".join(missing)
                return

            self.dut_serial = self.resources.dut_probe_sn
            self.tester_serial = self.resources.tester_probe_sn
            self.js_interface = self.resources.joulescope_serial
            self.initialized = True
            self.state = BenchState.IDLE
            self.startup_error = ""
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.initialized = False
            self.state = BenchState.ERROR
            self.startup_error = str(exc)

    def health_check(self) -> BenchHealthReport:
        """Inspect bench health and report whether the hardware is ready."""

        if self.identity is None or self.resources is None:
            self.initialize()

        identity = self.identity or BenchIdentity(bench_id=platform.node(), hostname=platform.node())
        checks: list[BenchCheckResult] = []

        if self.startup_error:
            checks.append(
                BenchCheckResult(
                    name="bench_startup",
                    status=BenchCheckStatus.FAIL,
                    summary="bench startup failed",
                    detail=self.startup_error,
                )
            )
        else:
            checks.append(
                BenchCheckResult(
                    name="bench_startup",
                    status=BenchCheckStatus.PASS if self.initialized else BenchCheckStatus.FAIL,
                    summary="bench initialized" if self.initialized else "bench not initialized",
                )
            )

        resources = self.resources or BenchResources()
        checks.extend(
            [
                _presence_check("board", resources.board, "board configured"),
                _presence_check("device", resources.device, "device configured"),
                _presence_check("dut_probe", resources.dut_probe_sn, "DUT probe configured"),
                _presence_check("tester_probe", resources.tester_probe_sn, "tester probe configured"),
                _presence_check(
                    "dut_serial_port",
                    resources.dut_serial_port,
                    "DUT serial mapping configured",
                ),
                _presence_check(
                    "tester_serial_port",
                    resources.tester_serial_port,
                    "tester serial mapping configured",
                ),
                _optional_presence_check(
                    "joulescope",
                    resources.joulescope_serial,
                    "Joulescope mapping configured",
                    "Joulescope mapping is not configured",
                ),
                _tool_check("commander", resources.commander_path),
                _optional_tool_check(
                    "xapi",
                    resources.xapi_path,
                    "client-supplied sl_bt.xapi will be used for remote tests",
                ),
                BenchCheckResult(
                    name="bench_state",
                    status=BenchCheckStatus.PASS
                    if self.state in {BenchState.IDLE, BenchState.OFFLINE}
                    else BenchCheckStatus.WARN
                    if self.state in {BenchState.BUSY, BenchState.TESTING}
                    else BenchCheckStatus.FAIL,
                    summary=f"bench state is {self.state.value}",
                ),
            ]
        )

        state = _overall_bench_state(checks, self.state)
        return BenchHealthReport(
            identity=identity,
            state=state,
            dut_state=_device_state(checks, "dut_"),
            tester_state=_device_state(checks, "tester_"),
            js_state=DeviceState.READY
            if _check_status(checks, "joulescope") is not BenchCheckStatus.FAIL
            else DeviceState.ERROR,
            summary=_health_summary(state, checks),
            checks=checks,
        )

    def inspect(self, measurement_id: str) -> None:
        """Inspect the images and measurement clearance for the client."""
        if not measurement_id:
            raise ValueError("measurement_id is required")

    def run(self, measurement_id: str) -> None:
        """Run a previously accepted measurement."""
        self.inspect(measurement_id)

    def release(self, measurement_id: str) -> None:
        """Return the bench to idle after the active measurement completes or aborts."""
        self.inspect(measurement_id)
        self.state = BenchState.IDLE

    def fw_save(self, file: Any) -> None:
        """Save the CMS Client sent FW to RAM storage."""
        raise NotImplementedError("firmware upload streams are staged by cmsd.py")

    def fw_is_valid(self, session: BenchSession) -> bool:
        """
        Run the smoke tests on the FW so that if the check passes then
        the tests are safe to run, send a response to the client about
        the validity of its FW Images.

        Before running the smoke tests, check if all images available
        are recent, as, after the Client connected.
        """
        try:
            self.test_prepare(session)
        except RuntimeError as exc:
            session.notes.append(str(exc))
            return False
        return True

    def test_prepare(self, session: BenchSession) -> None:
        """Validate the staged firmware and map it into the local test layout."""
        if self.resources is None:
            raise RuntimeError("bench resources are not initialized")
        if session.request.firmware.git_commit == "":
            session.notes.append("firmware git_commit was not supplied")
        if session.request.firmware.sisdk_version == "":
            session.notes.append("firmware sisdk_version was not supplied")
        if session.manifest.board and session.manifest.board != self.resources.board:
            raise RuntimeError(
                f"firmware board {session.manifest.board} does not match bench board {self.resources.board}"
            )
        if session.manifest.device and session.manifest.device != self.resources.device:
            raise RuntimeError(
                f"firmware device {session.manifest.device} does not match bench device {self.resources.device}"
            )
        if self.resources.board and session.request.selector.mode == "":
            raise RuntimeError("test selector mode is required")

        dut_path = session.runtime_dir / session.manifest.dut_img.name
        tester_path = session.runtime_dir / session.manifest.tester_img.name
        _validate_firmware_file(dut_path, session.manifest.dut_img.sha256 or session.manifest.dut_img_hash)
        _validate_firmware_file(tester_path, session.manifest.tester_img.sha256 or session.manifest.tester_img_hash)
        for name, image in session.manifest.dut_variants.items():
            _validate_firmware_file(
                session.runtime_dir / image.name,
                image.sha256,
            )
        if session.manifest.bootloader_img is not None:
            bootloader_path = session.runtime_dir / session.manifest.bootloader_img.name
            _validate_firmware_file(
                bootloader_path,
                session.manifest.bootloader_img.sha256 or session.manifest.bootloader_img_hash,
            )
        if session.manifest.xapi_file is None:
            raise RuntimeError("sl_bt.xapi was not supplied by the CMS client")
        xapi_path = session.runtime_dir / session.manifest.xapi_file.name
        _validate_firmware_file(
            xapi_path,
            session.manifest.xapi_file.sha256 or session.manifest.xapi_file_hash,
        )
        _validate_variable_filtered_campaign(session)

    def persist_staged_firmware_provenance(self, session: BenchSession) -> None:
        """Record the validated client SDK provenance in persistent bench config."""

        config = _load_json_with_trailing_commas(self.config_path)
        ble = config.setdefault("ble", {})
        if not isinstance(ble, dict):
            raise RuntimeError("cms bench runtime config field 'ble' must be an object")
        firmware = session.request.firmware
        ble["sdk"] = {
            "name": firmware.sisdk_name,
            "source": firmware.sisdk_source,
            "version": firmware.sisdk_version,
            "commit": firmware.sisdk_commit,
            "hash": firmware.sisdk_hash,
            "xapi_sha256": firmware.xapi_file_hash,
        }
        temporary_path = self.config_path.with_name(self.config_path.name + ".tmp")
        temporary_path.write_text(
            json.dumps(config, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, self.config_path)

    def test_smoke(self, session: BenchSession) -> None:
        """Run a lightweight preflight before spending time on a full run."""
        if not self.initialized:
            raise RuntimeError("bench is not initialized")
        if self.state not in {BenchState.IDLE, BenchState.BUSY, BenchState.TESTING}:
            raise RuntimeError(f"bench is not runnable from state {self.state.value}")
        self.test_prepare(session)

    def measurement_run(self, session: BenchSession) -> MeasurementResult:
        """Run the requested measurement flow and return structured results."""
        started_at = datetime.utcnow()
        self.state = BenchState.TESTING
        try:
            self.test_smoke(session)
            prepared = _prepare_pytest_runtime(session, self.resources or BenchResources())
            completed = _run_test_orchestrator(session, prepared["results_log"], prepared["metadata_path"])
            result = MeasurementResult(
                measurement_id=session.measurement_id,
                state=MeasurementState.PASSED if completed.returncode == 0 else MeasurementState.FAILED,
                started_at=started_at,
                finished_at=datetime.utcnow(),
                progress_percent=90,
                current_step=MeasurementState.COLLECTING_ARTIFACTS.value,
                message="test execution completed" if completed.returncode == 0 else "test execution failed",
                identity=session.request.identity,
            )
            run_summary_path = session.runtime_dir / "cms_test_run.json"
            run_summary_path.write_text(
                json.dumps(
                    {
                        "request": measurement_request_to_dict(session.request),
                        "returncode": completed.returncode,
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                        "notes": session.notes,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            return self.collect_artifacts(session, result, artifact_since=prepared["artifact_since"])
        except RuntimeError as exc:
            return MeasurementResult(
                measurement_id=session.measurement_id,
                state=MeasurementState.FAILED,
                started_at=started_at,
                finished_at=datetime.utcnow(),
                progress_percent=100,
                current_step=MeasurementState.FAILED.value,
                message=str(exc),
                identity=session.request.identity,
            )
        finally:
            self.state = BenchState.IDLE

    def collect_artifacts(
        self,
        session: BenchSession,
        result: MeasurementResult,
        *,
        artifact_since: float | None = None,
    ) -> MeasurementResult:
        """Attach local artifact metadata after the execution step finishes."""
        for path in _run_diagnostic_artifacts(session):
            relative_path = path.relative_to(session.runtime_dir)
            result_type = (
                ResultType.PYTEST_JUNIT
                if relative_path.parts and relative_path.parts[0] == "junit"
                else ResultType.DIAGNOSTIC
            )
            result.artifacts.append(
                Artifacts(
                    name=(Path("cmsd") / session.measurement_id / relative_path).as_posix(),
                    path=path,
                    result_type=result_type,
                )
            )
        for path in _real_test_artifacts(session, since=artifact_since):
            result.artifacts.append(
                Artifacts(
                    name=_artifact_name(Path("artifacts"), path),
                    path=path,
                    result_type=ResultType.CURRENT_MEASUREMENT,
                )
            )
        result.progress_percent = 100
        if result.state is MeasurementState.PASSED:
            result.current_step = MeasurementState.PASSED.value
            result.message = "test artifacts collected"
        return result

    def init(self, dev_config: dict[str, Any], js: Any = None) -> BenchResources:
        """Compatibility initializer for the previous local bench API."""

        resources = resolve_cms_resources_from_dev_config(dev_config)
        if js is not None:
            resources.joulescope_serial = str(js)
            self.js_interface = js
        self.resources = resources
        self.identity = BenchIdentity(
            bench_id=platform.node(),
            hostname=platform.node(),
            board=resources.board,
            device=resources.device,
        )
        self.dut_serial = resources.dut_probe_sn
        self.tester_serial = resources.tester_probe_sn
        self.initialized = True
        self.state = BenchState.IDLE
        return resources

    def run_test(
        self,
        dut_app: str,
        tester_app: str,
        analysis: CMSBenchResultAnalysis = CMSBenchResultAnalysis.DEFAULT,
        runner: Callable[[CMSBenchRunRequest], Any] | None = None,
    ) -> Any:
        """Compatibility runner for the previous local bench API."""

        request = CMSBenchRunRequest(dut_app=dut_app, tester_app=tester_app, analysis=analysis)
        self.state = BenchState.TESTING
        try:
            if runner is None:
                return analysis
            return runner(request)
        finally:
            self.state = BenchState.IDLE

    def _missing_required_config(self) -> list[str]:
        identity = self.identity or BenchIdentity(bench_id="")
        resources = self.resources or BenchResources()
        required = {
            "bench_id": identity.bench_id,
            "board": resources.board,
            "device": resources.device,
            "dut.probe_sn": resources.dut_probe_sn,
            "tester.probe_sn": resources.tester_probe_sn,
            "dut.serial_port": resources.dut_serial_port,
            "tester.serial_port": resources.tester_serial_port,
            "commander": resources.commander_path,
        }
        return [name for name, value in required.items() if not str(value).strip()]


def load_bench_runtime_config(path: str | Path = CMS_BENCH_CONFIG) -> tuple[BenchIdentity, BenchResources]:
    """Load persistent bench identity and hardware mappings from JSON."""

    config_path = Path(path)
    config = _load_json_with_trailing_commas(config_path)

    bench = config.get("bench", {})
    devices = config.get("devices", {})
    dut = config.get("dut", {})
    tester = config.get("tester", {})
    joulescope = config.get("joulescope", config.get("Joulescope", {}))
    tools = config.get("tools", {})
    ble = config.get("ble", {})

    hostname = platform.node()
    identity = BenchIdentity(
        bench_id=str(bench.get("bench_id", config.get("bench_id", config.get("bench-id", hostname)))),
        board=str(devices.get("board", config.get("board", ""))),
        device=str(devices.get("device", config.get("device", ""))),
        hostname=str(bench.get("hostname", hostname)),
        profile=str(bench.get("profile", config.get("profile", "bench-lite"))),
    )
    resources = BenchResources(
        dut_probe_sn=str(dut.get("probe_sn", "")),
        tester_probe_sn=str(tester.get("probe_sn", "")),
        dut_serial_port=str(dut.get("serial_port", "")),
        tester_serial_port=str(tester.get("serial_port", "")),
        joulescope_serial=str(
            joulescope.get("serial_number", joulescope.get("serial_port", joulescope.get("id", "")))
        ),
        joulescope_wake_gpio=_optional_int(joulescope.get("wake_gpio")),
        board=identity.board,
        board_rev=str(devices.get("board_rev", devices.get("brev", ""))),
        device=identity.device,
        part_number=str(devices.get("part_number", devices.get("device", ""))),
        serial_number=str(devices.get("serial_number", "")),
        result_db=str(config.get("result_db", "")),
        commander_path=str(tools.get("commander", config.get("commander", ""))),
        xapi_path=str(ble.get("xapi", tools.get("xapi", config.get("xapi", "")))),
    )
    return identity, resources


def resolve_cms_resources_from_dev_config(dev_config: dict[str, Any]) -> BenchResources:
    """Resolve bench resource mappings from the repo's development config shape."""

    firmware = dev_config.get("firmware", dev_config.get("devices", {}))
    joulescope = dev_config.get("Joulescope", dev_config.get("joulescope", {}))
    tools = dev_config.get("tools", {})
    ble = dev_config.get("ble", {})
    return BenchResources(
        dut_probe_sn=str(dev_config.get("dut", {}).get("probe_sn", "")),
        tester_probe_sn=str(dev_config.get("tester", {}).get("probe_sn", "")),
        dut_serial_port=str(dev_config.get("dut", {}).get("serial_port", "")),
        tester_serial_port=str(dev_config.get("tester", {}).get("serial_port", "")),
        joulescope_serial=str(
            joulescope.get("serial_number", joulescope.get("serial_port", joulescope.get("id", "")))
        ),
        joulescope_wake_gpio=_optional_int(joulescope.get("wake_gpio")),
        board=str(firmware.get("board", "")),
        board_rev=str(firmware.get("brev", firmware.get("board_rev", ""))),
        device=str(firmware.get("device", "")),
        part_number=str(firmware.get("part_number", firmware.get("device", ""))),
        serial_number=str(firmware.get("serial_number", "")),
        commander_path=str(tools.get("commander", dev_config.get("commander", ""))),
        xapi_path=str(ble.get("xapi", tools.get("xapi", dev_config.get("xapi", "")))),
    )


def resolve_cms_resources_from_hwmux(group_json: dict[str, Any]) -> BenchResources:
    """Resolve bench resources from a HWMux device-group payload."""

    metadata = group_json.get("metadata", {})
    devices = group_json.get("devices", [])
    by_role: dict[str, dict[str, Any]] = {}
    for device in devices:
        role = str(device.get("metadata", {}).get("Role", "")).upper()
        if not role:
            continue
        if role in by_role:
            raise RuntimeError(f"multiple devices with Role={role}")
        by_role[role] = device

    dut = by_role.get("DUT")
    tester = by_role.get("TESTER")
    joulescope = by_role.get("JOULESCOPE", {})
    if dut is None:
        raise RuntimeError("missing device with Role=DUT")
    if tester is None:
        raise RuntimeError("missing device with Role=TESTER")

    dut_metadata = dut.get("metadata", {})
    tester_metadata = tester.get("metadata", {})
    joulescope_metadata = joulescope.get("metadata", {})
    return BenchResources(
        group_id=group_json.get("id"),
        group_name=str(group_json.get("name", "")),
        dut_probe_sn=str(dut_metadata.get("jlink_serial_number", "")),
        tester_probe_sn=str(tester_metadata.get("jlink_serial_number", "")),
        dut_serial_port=str(dut_metadata.get("serial_port", "")),
        tester_serial_port=str(tester_metadata.get("serial_port", "")),
        joulescope_serial=str(joulescope_metadata.get("serial_number", "")),
        joulescope_wake_gpio=_optional_int(metadata.get("joulescope_dut_wake_gpio")),
        board=str(metadata.get("cms_board", dut.get("part", {}).get("board_no", ""))),
        board_rev=str(metadata.get("cms_board_rev", dut.get("part", {}).get("board_rev", ""))),
        device=str(metadata.get("cms_device", dut.get("part", {}).get("variant", ""))),
        part_number=str(
            metadata.get(
                "cms_part_number",
                metadata.get("cms_device", dut.get("part", {}).get("variant", "")),
            )
        ),
        serial_number=str(metadata.get("cms_serial_number", dut.get("serial_number", ""))),
    )


def _prepare_pytest_runtime(session: BenchSession, resources: BenchResources) -> dict[str, Path]:
    build_root = session.runtime_dir / "fw_build"
    config_data = _campaign_firmware_config(session)
    selected_config = (
        _write_selected_config_evidence(session)
        if session.request.selector.feature != "all"
        else {}
    )
    firmware = config_data.get("firmware", {})
    tester_project = build_root / Path(str(firmware.get("tester_slcp", session.manifest.tester_img.name))).stem
    dut_copied_images: dict[str, Path] = {}
    if session.manifest.dut_variants:
        dut_projects = {
            "dcdc": build_root / "ncp_ble_dut_dcdc",
            "no_dcdc": build_root / "ncp_ble_dut",
        }
        missing_variants = set(dut_projects) - set(session.manifest.dut_variants)
        if missing_variants:
            raise RuntimeError(
                "staged firmware is missing DUT variants: "
                + ", ".join(sorted(missing_variants))
            )
        for name, project in dut_projects.items():
            image = session.manifest.dut_variants[name]
            dut_copied_images[name] = _copy_image_to_project(
                session.runtime_dir / image.name,
                project,
            )
    else:
        dut_project = build_root / Path(
            str(firmware.get("dut_slcp", session.manifest.dut_img.name))
        ).stem
        dut_copied_images["default"] = _copy_image_to_project(
            session.runtime_dir / session.manifest.dut_img.name,
            dut_project,
        )
    tester_copied_image = _copy_image_to_project(
        session.runtime_dir / session.manifest.tester_img.name,
        tester_project,
    )
    bootloader_copied_image = _copy_bootloader_image(session, build_root)
    xapi_path = _staged_xapi_path(session)

    metadata = {
        "identity": (
            measurement_identity_to_dict(session.request.identity)
            if session.request.identity is not None
            else {
                "measurement_id": session.measurement_id,
                "run_name": session.measurement_id,
                "created_at": "",
            }
        ),
        "request": {
            "selector": {
                "mode": session.request.selector.mode,
                "feature": session.request.selector.feature,
                "config": session.request.selector.config,
                "config_hash": session.request.selector.config_hash,
                "variable_filters": variable_filters_to_dicts(
                    session.request.selector.variable_filters
                ),
            },
        },
        "firmware": {
            "build_root": str(build_root),
            "provision_state_root": str(Path("artifacts") / "cmsd" / "provision_state"),
            "board": resources.board,
            "brev": resources.board_rev,
            "board_rev": resources.board_rev,
            "board_revision": resources.board_rev,
            "part_number": resources.part_number or resources.device,
            "serial_number": resources.serial_number,
            "device": resources.device,
            "sisdk": session.manifest.sisdk_version,
            "sisdk_name": session.manifest.sisdk_name,
            "sisdk_source": session.manifest.sisdk_source,
            "sisdk_version": session.manifest.sisdk_version,
            "sisdk_commit": session.manifest.sisdk_commit,
            "sisdk_hash": session.manifest.sisdk_hash,
            "xapi_file_hash": session.manifest.xapi_file_hash,
            "dut_image": str(session.runtime_dir / session.manifest.dut_img.name),
            "tester_image": str(session.runtime_dir / session.manifest.tester_img.name),
            "dut_image_sha256": session.manifest.dut_img.sha256
            or session.manifest.dut_img_hash
            or _sha256_file(
                dut_copied_images["dcdc"]
                if "dcdc" in dut_copied_images
                else dut_copied_images["default"]
            ),
            "tester_image_sha256": session.manifest.tester_img.sha256
            or session.manifest.tester_img_hash
            or _sha256_file(tester_copied_image),
            "dut_variants": {
                name: {
                    "image": str(image),
                    "sha256": session.manifest.dut_variants[name].sha256
                    or _sha256_file(image),
                }
                for name, image in dut_copied_images.items()
                if name in session.manifest.dut_variants
            },
            "dut_slcp": str(firmware.get("dut_slcp", "")),
            "tester_slcp": str(firmware.get("tester_slcp", "")),
            "bootloader_image": str(bootloader_copied_image) if bootloader_copied_image is not None else "",
            "bootloader_image_sha256": (
                session.manifest.bootloader_img.sha256
                or session.manifest.bootloader_img_hash
                or _sha256_file(bootloader_copied_image)
            ) if session.manifest.bootloader_img is not None and bootloader_copied_image is not None else "",
        },
        "dut": {
            "serial_port": resources.dut_serial_port,
            "probe_sn": resources.dut_probe_sn,
        },
        "tester": {
            "serial_port": resources.tester_serial_port,
            "probe_sn": resources.tester_probe_sn,
        },
        "tools": {
            "commander": resources.commander_path,
        },
        "ble": {
            "xapi": str(xapi_path),
        },
    }
    metadata_path = session.runtime_dir / "cms_fw_metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    prepared = {
        "build_root": build_root,
        "results_log": session.runtime_dir / "orchestrator_results.jsonl",
        "metadata_path": metadata_path,
        "artifact_since": time.time(),
    }
    if selected_config:
        prepared["selected_config_path"] = selected_config["snapshot_path"]
        prepared["selected_config_metadata_path"] = selected_config["metadata_path"]
    return prepared


def _campaign_firmware_config(session: BenchSession) -> dict[str, Any]:
    """Return firmware metadata shared by the selected one- or all-feature campaign."""

    if session.request.selector.feature != "all":
        return _load_requested_test_config(session)

    config_root = Path("tests") / "configs" / session.request.selector.mode
    config_paths = sorted(config_root.glob("*/default.json"))
    if not config_paths:
        raise RuntimeError(f"no feature default configs exist beneath {config_root}")
    configs = []
    for config_path in config_paths:
        try:
            config_data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"test config is not valid JSON: {config_path}") from exc
        if not isinstance(config_data, dict):
            raise RuntimeError(f"test config must be a JSON object: {config_path}")
        firmware = config_data.get("firmware", {})
        if not isinstance(firmware, dict):
            raise RuntimeError(f"test config firmware section must be an object: {config_path}")
        configs.append((config_path, config_data))

    firmware_projects = {
        (
            str(config_data["firmware"].get("dut_slcp", "")),
            str(config_data["firmware"].get("tester_slcp", "")),
        )
        for _, config_data in configs
    }
    if len(firmware_projects) != 1:
        raise RuntimeError(
            "all-feature campaigns require identical DUT and tester firmware projects"
        )
    return configs[0][1]


def _staged_xapi_path(session: BenchSession) -> Path:
    if session.manifest.xapi_file is None:
        raise RuntimeError("sl_bt.xapi was not supplied by the CMS client")
    xapi_path = session.runtime_dir / session.manifest.xapi_file.name
    if not xapi_path.exists():
        raise RuntimeError(f"staged sl_bt.xapi is missing: {xapi_path}")
    return xapi_path


def _copy_image_to_project(source: Path, project_dir: Path) -> Path:
    if not source.exists():
        raise RuntimeError(f"staged firmware image is missing: {source}")
    destination = project_dir / "cmake_gcc" / "build" / "base" / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def _copy_bootloader_image(session: BenchSession, build_root: Path) -> Path | None:
    if session.manifest.bootloader_img is None:
        return None
    source = session.runtime_dir / session.manifest.bootloader_img.name
    return _copy_image_to_project(source, build_root / "bootloader-uart-bgapi")


def _run_managed_process(
    command: list[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    event_stream: MeasurementEventStream | None = None,
    log_max_bytes: int = DEFAULT_CONSOLE_LOG_MAX_BYTES,
    timeout_s: float = DEFAULT_EXECUTION_TIMEOUT_S,
    cancel_event: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a child while draining both output pipes into active runtime logs."""

    if log_max_bytes < 1:
        raise ValueError("log_max_bytes must be positive")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    child_environment = os.environ.copy()
    child_environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=child_environment,
        start_new_session=platform.system() == "Linux",
    )
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("managed child process did not expose stdout and stderr pipes")

    if event_stream is not None:
        event_stream.append_lifecycle(
            "process_started",
            pid=process.pid,
            command=command,
        )
        event_stream.replace_status(
            {
                "state": "running",
                "pid": process.pid,
                "command": command,
            }
        )

    drain_errors: list[OSError] = []
    stdout_thread = threading.Thread(
        target=_drain_process_stream,
        args=(
            process.stdout,
            stdout_path,
            "stdout",
            event_stream,
            log_max_bytes,
            drain_errors,
        ),
        name="cms-stdout-drain",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_process_stream,
        args=(
            process.stderr,
            stderr_path,
            "stderr",
            event_stream,
            log_max_bytes,
            drain_errors,
        ),
        name="cms-stderr-drain",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    cancelled = False
    deadline = time.monotonic() + timeout_s
    while True:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            if event_stream is not None:
                event_stream.append_lifecycle(
                    "process_cancellation_requested",
                    pid=process.pid,
                )
                event_stream.replace_status(
                    {
                        "state": "process_cancellation_requested",
                        "pid": process.pid,
                    }
                )
            _terminate_process_tree(process)
            returncode = process.returncode if process.returncode is not None else -1
            break

        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            timed_out = True
            break
        try:
            returncode = process.wait(timeout=min(0.1, remaining_s))
            break
        except subprocess.TimeoutExpired:
            continue

    if timed_out:
        if event_stream is not None:
            event_stream.append_lifecycle(
                "process_timed_out",
                pid=process.pid,
                timeout_s=timeout_s,
            )
            event_stream.replace_status(
                {
                    "state": "process_timed_out",
                    "pid": process.pid,
                    "timeout_s": timeout_s,
                }
            )
        _terminate_process_tree(process)
        returncode = process.returncode if process.returncode is not None else -1
    stdout_thread.join()
    stderr_thread.join()
    if event_stream is not None and cancelled:
        event_stream.append_lifecycle(
            "process_cancelled",
            returncode=returncode,
            output_error=bool(drain_errors),
        )
        event_stream.replace_status(
            {
                "state": "process_cancelled",
                "returncode": returncode,
                "output_error": bool(drain_errors),
            }
        )
    elif event_stream is not None and not timed_out:
        event_stream.append_lifecycle(
            "process_completed",
            returncode=returncode,
            output_error=bool(drain_errors),
        )
        event_stream.replace_status(
            {
                "state": "process_completed",
                "returncode": returncode,
                "output_error": bool(drain_errors),
            }
        )
    if drain_errors:
        detail = "; ".join(str(error) for error in drain_errors)
        raise RuntimeError(f"failed to drain child process output: {detail}")
    if timed_out:
        raise RuntimeError(f"orchestrator timed out after {timeout_s:g} seconds")

    return subprocess.CompletedProcess(
        command,
        returncode,
        stdout=stdout_path.read_text(encoding="utf-8"),
        stderr=stderr_path.read_text(encoding="utf-8"),
    )


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Stop a managed child and its Linux process group within a fixed grace period."""

    linux_process_group = platform.system() == "Linux"
    try:
        if linux_process_group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        process.wait()
        return

    try:
        process.wait(timeout=PROCESS_TERMINATE_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        if linux_process_group:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        process.wait()
        return
    process.wait()


def _drain_process_stream(
    stream,
    destination: Path,
    source: str,
    event_stream: MeasurementEventStream | None,
    log_max_bytes: int,
    errors: list[OSError],
) -> None:
    try:
        for line in stream:
            discarded_bytes = _append_bounded_log(
                destination,
                line,
                max_bytes=log_max_bytes,
            )
            if event_stream is not None:
                event_stream.append_console(source, line)
                if discarded_bytes:
                    event_stream.record_log_truncation(source, discarded_bytes)
    except OSError as exc:
        errors.append(exc)
    finally:
        stream.close()


def _run_test_orchestrator(
    session: BenchSession,
    results_log: Path,
    metadata_path: Path,
) -> subprocess.CompletedProcess[str]:
    feature = session.request.selector.feature
    if not feature:
        raise RuntimeError("test feature is required")
    config_dir = Path("tests") / "configs" / session.request.selector.mode
    if feature != "all":
        config_dir /= feature
    if not config_dir.exists():
        raise RuntimeError(f"test config directory does not exist: {config_dir}")

    orchestrator_mode = _orchestrator_mode(session)
    cmd = [
        sys.executable,
        "-u",
        "test_orchestrator.py",
        str(config_dir),
        "--mode",
        orchestrator_mode,
        "--results-log",
        str(results_log),
        "--cms-runtime-config",
        str(Path(CMS_BENCH_CONFIG)),
        "--cms-fw-metadata",
        str(metadata_path),
        "--log-file",
        str(session.runtime_dir / "pytest.log"),
        "--log-file-level",
        "INFO",
        "--log-file-mode",
        "w",
    ]
    if feature != "all":
        cmd.extend(["--feature", feature])
    if session.request.selector.config != "default":
        cmd.extend(["--exact-config", session.request.selector.config])
    for variable_filter in session.request.selector.variable_filters:
        cmd.extend(["--variable", f"{variable_filter.name}={variable_filter.value}"])
    if session.request.upload_results_to_database:
        cmd.append("--upload-results-to-database")

    return _run_managed_process(
        cmd,
        cwd=Path("."),
        stdout_path=session.runtime_paths.stdout,
        stderr_path=session.runtime_paths.stderr,
        event_stream=session.event_stream,
        log_max_bytes=session.max_console_log_bytes,
        timeout_s=session.execution_timeout_s,
        cancel_event=session.cancel_event,
    )


def _validate_variable_filtered_campaign(session: BenchSession) -> None:
    variable_filters = session.request.selector.variable_filters
    if not variable_filters:
        return
    if session.request.selector.config != "default" or session.request.selector.config_hash:
        raise RuntimeError(
            "variable filters cannot be combined with an exact config selection"
        )
    feature = session.request.selector.feature
    config_dir = Path("tests") / "configs" / session.request.selector.mode / feature
    orchestrator_mode = _orchestrator_mode(session)
    try:
        from test_orchestrator import build_jobs, validate_variable_filters_for_targets

        validate_variable_filters_for_targets(
            [config_dir],
            feature_filters={feature},
            variable_filters=variable_filters,
        )
        jobs = build_jobs(
            [config_dir],
            mode=orchestrator_mode,
            feature_filters={feature},
            partition_filters=set(),
            variable_filters=variable_filters,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if not jobs:
        filters_text = ", ".join(
            f"{variable_filter.name}={variable_filter.value}"
            for variable_filter in variable_filters
        )
        raise RuntimeError(
            f"variable filters selected zero configs before execution: {filters_text}"
        )


def _orchestrator_mode(session: BenchSession) -> str:
    mode = session.request.mode.value
    return "full_sweep" if mode == "full_sweep" else "default"


def _run_diagnostic_artifacts(session: BenchSession) -> list[Path]:
    names = [
        "cms_fw_metadata.json",
        "cms_test_run.json",
        "orchestrator_plan.json",
        "orchestrator_results.jsonl",
        "pytest.log",
    ]
    artifacts = [path for name in names if (path := session.runtime_dir / name).exists()]
    artifacts.extend(
        path
        for path in (session.runtime_paths.stdout, session.runtime_paths.stderr)
        if path.exists()
    )
    junit_dir = session.runtime_dir / "junit"
    if junit_dir.exists():
        artifacts.extend(sorted(junit_dir.glob("*.xml")))
    return artifacts


def _load_requested_test_config(session: BenchSession) -> dict[str, Any]:
    config_path = _requested_test_config_path(session)
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
    return config_data


def _write_selected_config_evidence(session: BenchSession) -> dict[str, Path]:
    config_path = _requested_test_config_path(session)
    config_dir = _requested_test_config_dir(session)
    observed_hash = _sha256_text_file_lf_normalized(config_path)
    snapshot_path = session.runtime_dir / "selected_config.json"
    metadata_path = session.runtime_dir / "selected_config_metadata.json"
    session.runtime_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_path, snapshot_path)
    metadata = {
        "mode": session.request.selector.mode,
        "feature": session.request.selector.feature,
        "config": session.request.selector.config,
        "selected_config": config_path.relative_to(config_dir).as_posix(),
        "requested_config_hash": session.request.selector.config_hash,
        "observed_config_hash": observed_hash,
        "source_path": str(config_path),
        "snapshot_path": str(snapshot_path),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return {
        "snapshot_path": snapshot_path,
        "metadata_path": metadata_path,
    }


def _requested_test_config_dir(session: BenchSession) -> Path:
    feature = session.request.selector.feature.strip()
    if not feature:
        raise RuntimeError("test feature is required")
    if Path(feature).name != feature or feature in {".", ".."}:
        raise RuntimeError("test feature must be a single feature name")
    config_dir = Path("tests") / "configs" / session.request.selector.mode / feature
    if not config_dir.is_dir():
        raise RuntimeError(f"test feature config directory does not exist: {config_dir}")
    return config_dir.resolve()


def _requested_test_config_path(session: BenchSession) -> Path:
    config_dir = _requested_test_config_dir(session)
    config_name = session.request.selector.config or "default"
    config_name = config_name.strip().replace("\\", "/")
    if config_name.endswith(".json"):
        requested = config_name
    else:
        requested = f"{config_name}.json"
    relative = Path(requested)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise RuntimeError("test config must be a single relative config path")
    resolved = (config_dir / relative).resolve()
    try:
        resolved.relative_to(config_dir)
    except ValueError as exc:
        raise RuntimeError("test config must stay beneath the selected feature directory") from exc
    return resolved


def _real_test_artifacts(session: BenchSession, *, since: float | None = None) -> list[Path]:
    feature = session.request.selector.feature
    mode = "full_sweep" if session.request.mode.value == "full_sweep" else "default"
    feature_root = Path("artifacts") / session.request.selector.mode
    candidates = [
        feature_root if feature == "all" else feature_root / feature / mode,
        Path("artifacts") / "orchestrator",
    ]
    artifacts: list[Path] = []
    for candidate in candidates:
        if candidate.exists():
            artifacts.extend(
                path
                for path in sorted(candidate.rglob("*"))
                if path.is_file() and (since is None or path.stat().st_mtime >= since)
            )
    return artifacts


def _artifact_name(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _validate_firmware_file(path: Path, expected_sha256: str = "") -> None:
    if not path.exists():
        raise RuntimeError(f"firmware image is missing: {path}")
    if not path.is_file():
        raise RuntimeError(f"firmware image is not a file: {path}")
    if expected_sha256 and _sha256_file(path).lower() != expected_sha256.lower():
        raise RuntimeError(f"firmware image hash mismatch: {path.name}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text_file_lf_normalized(path: Path) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(data).hexdigest()


def _append_bounded_log(path: Path, text: str, *, max_bytes: int) -> int:
    encoded = text.encode("utf-8")
    current_size = path.stat().st_size if path.exists() else 0
    if current_size + len(encoded) <= max_bytes:
        with path.open("ab") as output:
            output.write(encoded)
            output.flush()
        return 0

    combined = (path.read_bytes() if path.exists() else b"") + encoded
    target_bytes = max(1, max_bytes // 2)
    retained = combined[-target_bytes:].decode("utf-8", errors="ignore").encode("utf-8")
    temporary_path = path.with_name(path.name + ".tmp")
    temporary_path.write_bytes(retained)
    os.replace(temporary_path, path)
    return len(combined) - len(retained)


def _bounded_console_payload(
    record: dict[str, Any],
    max_bytes: int,
) -> tuple[bytes, dict[str, Any]]:
    def serialize(candidate: dict[str, Any]) -> bytes:
        return (json.dumps(candidate, separators=(",", ":")) + "\n").encode("utf-8")

    payload = serialize(record)
    if len(payload) <= max_bytes:
        return payload, record

    original_text = str(record.get("text", ""))
    low = 0
    high = len(original_text)
    best_record = {**record, "text": "", "text_truncated": True}
    best_payload = serialize(best_record)
    if len(best_payload) > max_bytes:
        raise ValueError("max_console_bytes is too small for console event metadata")
    while low <= high:
        retained_characters = (low + high) // 2
        candidate = {
            **record,
            "text": original_text[-retained_characters:] if retained_characters else "",
            "text_truncated": True,
        }
        candidate_payload = serialize(candidate)
        if len(candidate_payload) <= max_bytes:
            best_record = candidate
            best_payload = candidate_payload
            low = retained_characters + 1
        else:
            high = retained_characters - 1
    return best_payload, best_record


def _console_segment_paths(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.jsonl") if path.is_file())


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _last_event_cursor(paths: MeasurementRuntimePaths) -> int:
    records = _read_jsonl_records(paths.events)
    for segment in _console_segment_paths(paths.console_events):
        records.extend(_read_jsonl_records(segment))
    return max((int(record.get("cursor", 0)) for record in records), default=0)


def _first_console_cursor(segments: list[Path]) -> int:
    for segment in segments:
        records = _read_jsonl_records(segment)
        if records:
            return int(records[0].get("cursor", 0))
    return 0


def _truncated_log_sources(path: Path) -> set[str]:
    return {
        str(record.get("source"))
        for record in _read_jsonl_records(path)
        if record.get("state") == "log_truncated" and record.get("source")
    }


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _presence_check(name: str, value: str, ok_summary: str) -> BenchCheckResult:
    if str(value).strip():
        return BenchCheckResult(name=name, status=BenchCheckStatus.PASS, summary=ok_summary)
    return BenchCheckResult(
        name=name,
        status=BenchCheckStatus.FAIL,
        summary=f"{name} is not configured",
    )


def _optional_presence_check(
    name: str,
    value: str,
    ok_summary: str,
    missing_summary: str,
) -> BenchCheckResult:
    if str(value).strip():
        return BenchCheckResult(name=name, status=BenchCheckStatus.PASS, summary=ok_summary)
    return BenchCheckResult(name=name, status=BenchCheckStatus.WARN, summary=missing_summary)


def _tool_check(name: str, value: str) -> BenchCheckResult:
    if not str(value).strip():
        return BenchCheckResult(name=name, status=BenchCheckStatus.FAIL, summary=f"{name} is not configured")

    resolved = shutil.which(value)
    if resolved:
        return BenchCheckResult(
            name=name,
            status=BenchCheckStatus.PASS,
            summary=f"{name} resolved on PATH",
            detail=resolved,
        )

    path = Path(value)
    if path.exists():
        return BenchCheckResult(
            name=name,
            status=BenchCheckStatus.PASS,
            summary=f"{name} path exists",
            detail=str(path),
        )

    return BenchCheckResult(
        name=name,
        status=BenchCheckStatus.FAIL,
        summary=f"{name} path is not available",
        detail=str(path),
    )


def _optional_tool_check(name: str, value: str, missing_summary: str) -> BenchCheckResult:
    if not str(value).strip():
        return BenchCheckResult(name=name, status=BenchCheckStatus.WARN, summary=missing_summary)
    check = _tool_check(name, value)
    if check.status is BenchCheckStatus.FAIL:
        check.status = BenchCheckStatus.WARN
    return check


def _load_json_with_trailing_commas(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        without_trailing_commas = re.sub(r",(\s*[}\]])", r"\1", raw)
        return json.loads(without_trailing_commas)


def _check_status(checks: list[BenchCheckResult], name: str) -> BenchCheckStatus:
    for check in checks:
        if check.name == name:
            return check.status
    return BenchCheckStatus.FAIL


def _device_state(checks: list[BenchCheckResult], prefix: str) -> DeviceState:
    related = [check for check in checks if check.name.startswith(prefix)]
    if related and all(check.status is BenchCheckStatus.PASS for check in related):
        return DeviceState.READY
    return DeviceState.ERROR


def _overall_bench_state(checks: list[BenchCheckResult], current_state: BenchState) -> BenchState:
    if any(check.status is BenchCheckStatus.FAIL for check in checks):
        return BenchState.ERROR
    if current_state in {BenchState.BUSY, BenchState.TESTING}:
        return BenchState.BUSY
    return BenchState.IDLE


def _health_summary(state: BenchState, checks: list[BenchCheckResult]) -> str:
    failed = [check.name for check in checks if check.status is BenchCheckStatus.FAIL]
    warned = [check.name for check in checks if check.status is BenchCheckStatus.WARN]
    if failed:
        return "bench health failed: " + ", ".join(failed)
    if warned:
        return "bench health warning: " + ", ".join(warned)
    if state is BenchState.BUSY:
        return "bench busy"
    return "bench ready"

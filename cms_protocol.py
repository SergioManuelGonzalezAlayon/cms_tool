"""Shared protocol models for the CMS client, daemon, and bench layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import re
import secrets
from typing import Any


CMS_OBSERVATION_PROTOCOL_VERSION = 1


class DeviceState(str, Enum):
    READY = "ready"
    ERROR = "error"

class BenchState(str, Enum):
    """Runtime state of one physical bench."""
    IDLE = "idle"
    BUSY = "busy"
    TESTING = "testing"
    OFFLINE = "offline"
    ERROR = "error"


class BenchCheckStatus(str, Enum):
    """Result of one individual health-check item."""
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class MeasurementState(str, Enum):
    """Lifecycle states visible to the remote client."""
    ACCEPTED = "accepted"
    WAITING_FOR_ARTIFACTS = "waiting_for_artifacts"
    VALIDATING_FIRMWARE = "validating_firmware"
    FLASHING = "flashing"
    SMOKE_TEST = "smoke_test"
    RUNNING = "running"
    COLLECTING_ARTIFACTS = "collecting_artifacts"
    UPLOADING_RESULTS = "uploading_results"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    INTERRUPTED = "interrupted"


class MeasurementMode(str, Enum):
    """High-level measurement profile requested by the client."""
    DEBUG = "debug"
    DEFAULT = "default"
    FULL_SWEEP = "full_sweep"
    DIFF = "diff"


class TestCaseState(str, Enum):
    """Lifecycle states for one planned test case."""
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ResultType(str, Enum):
    """Distinct result layers produced by one CMS campaign."""
    PYTEST_JUNIT = "pytest_junit"
    CURRENT_MEASUREMENT = "current_measurement"
    CAMPAIGN = "campaign"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True, slots=True)
class MeasurementIdentity:
    """Stable identity shared by every record belonging to one execution."""
    measurement_id: str
    run_name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TestCaseIdentity:
    """Stable identity and ordering metadata for one planned test case."""
    job_id: str
    ordinal: int
    total_jobs: int
    feature: str
    config: str
    config_hash: str = ""
    mode: str = ""
    partition: str = ""

    def __post_init__(self) -> None:
        if self.ordinal < 1:
            raise ValueError("test case ordinal must be at least 1")
        if self.total_jobs < 1:
            raise ValueError("total jobs must be at least 1")
        if self.ordinal > self.total_jobs:
            raise ValueError("test case ordinal cannot exceed total jobs")


@dataclass(frozen=True, slots=True)
class TestCounts:
    """Campaign counters; cancelled jobs are included in ``completed``."""
    total: int = 0
    completed: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0

    def __post_init__(self) -> None:
        values = (self.total, self.completed, self.passed, self.failed, self.skipped)
        if any(value < 0 for value in values):
            raise ValueError("test counts cannot be negative")
        if self.completed > self.total:
            raise ValueError("completed test count cannot exceed total")
        if self.passed + self.failed + self.skipped > self.completed:
            raise ValueError("terminal outcome counts cannot exceed completed")

    @property
    def remaining(self) -> int:
        """Number of planned test cases without a terminal record."""
        return self.total - self.completed


@dataclass(frozen=True, order=True, slots=True)
class EventCursor:
    """Non-negative position in a per-measurement append-only event stream."""
    value: int = 0

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("event cursor cannot be negative")

    def next(self) -> "EventCursor":
        """Return the next monotonically increasing cursor."""
        return EventCursor(self.value + 1)


@dataclass(frozen=True, slots=True)
class MeasurementEvent:
    """One typed record from a measurement's reconnectable event stream."""

    measurement_id: str
    cursor: EventCursor
    timestamp: datetime
    event_type: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.measurement_id:
            raise ValueError("measurement event requires a measurement id")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("measurement event timestamp must include a timezone")
        if not self.event_type:
            raise ValueError("measurement event requires an event type")


@dataclass(frozen=True, slots=True)
class MeasurementSummary:
    """Concise authoritative state for reconnecting measurement observers."""

    measurement_id: str
    state: MeasurementState
    counts: TestCounts
    identity: MeasurementIdentity | None = None
    current_test: TestCaseIdentity | None = None
    failed_tests: tuple[TestCaseIdentity, ...] = ()
    protocol_version: int = CMS_OBSERVATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version < 1:
            raise ValueError("observation protocol version must be positive")
        if not self.measurement_id:
            raise ValueError("measurement summary requires a measurement id")
        if self.identity is not None and self.identity.measurement_id != self.measurement_id:
            raise ValueError("measurement summary identity must match its measurement id")
        if self.current_test is not None and self.current_test.total_jobs != self.counts.total:
            raise ValueError("current test total must match summary counts")
        if len(self.failed_tests) != self.counts.failed:
            raise ValueError("failed test identities must match the failed count")
        if any(test.total_jobs != self.counts.total for test in self.failed_tests):
            raise ValueError("failed test totals must match summary counts")


@dataclass(frozen=True, slots=True)
class MeasurementEventPage:
    """Retained measurement events returned after a client cursor."""

    measurement_id: str
    events: tuple[MeasurementEvent, ...]
    next_cursor: EventCursor
    oldest_retained_cursor: EventCursor
    truncated: bool = False
    protocol_version: int = CMS_OBSERVATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version < 1:
            raise ValueError("observation protocol version must be positive")
        if not self.measurement_id:
            raise ValueError("measurement event page requires a measurement id")
        if any(event.measurement_id != self.measurement_id for event in self.events):
            raise ValueError("measurement event ids must match their page")
        cursors = [event.cursor.value for event in self.events]
        if any(current >= following for current, following in zip(cursors, cursors[1:])):
            raise ValueError("measurement event cursors must be strictly increasing")
        if self.events and self.next_cursor != self.events[-1].cursor:
            raise ValueError("next cursor must identify the last returned event")


@dataclass(slots=True)
class BenchIdentity:
    """Stable metadata that identifies one CMS bench."""
    bench_id: str
    board: str = ""
    device: str = ""
    hostname: str = ""
    profile: str = "bench-lite"


@dataclass(slots=True)
class FirmwareImage:
    """One remotely supplied firmware asset."""
    name: str
    url: str
    sha256: str = ""


@dataclass(slots=True)
class FirmwareData:
    """Firmware references that the daemon stages before execution."""
    dut_img: FirmwareImage
    tester_img: FirmwareImage
    board: str = ""
    device: str = ""
    dut_img_hash: str = ""
    tester_img_hash: str = ""
    bootloader_img: FirmwareImage | None = None
    bootloader_img_hash: str = ""
    xapi_file: FirmwareImage | None = None
    xapi_file_hash: str = ""
    git_commit: str = ""
    sisdk_source: str = ""
    sisdk_version: str = ""
    sisdk_commit: str = ""
    sisdk_hash: str = ""


@dataclass(slots=True)
class SuiteSelector:
    """Select the suite, feature, and config variant to execute on the bench."""
    mode: str
    feature: str
    config: str = "default"
    config_hash: str = ""
    variable_filters: tuple["VariableFilter", ...] = ()


@dataclass(frozen=True, slots=True)
class VariableFilter:
    """One generated-config parameter/value filter requested by the operator."""
    name: str
    value: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("variable filter name must not be empty")
        if not self.value.strip():
            raise ValueError("variable filter value must not be empty")
        if "=" in self.name:
            raise ValueError("variable filter name must not contain '='")


@dataclass(slots=True)
class MeasurementRequest:
    """One remote measurement request submitted by the client."""
    request_id: str
    selector: SuiteSelector
    firmware: FirmwareData
    mode: MeasurementMode = MeasurementMode.DEFAULT
    requested_by: str = ""
    upload_results_to_database: bool = False
    identity: MeasurementIdentity | None = None


@dataclass(slots=True)
class Artifacts:
    """Artifact metadata returned once a measurement completes."""
    name: str
    path: Path | None = None
    url: str = ""
    result_type: ResultType = ResultType.DIAGNOSTIC


@dataclass(slots=True)
class MeasurementResult:
    """Structured measurement outcome returned by the bench layer."""
    measurement_id: str
    state: MeasurementState
    started_at: datetime | None = None
    finished_at: datetime | None = None
    artifacts: list[Artifacts] = field(default_factory=list)
    progress_percent: int = 0
    current_step: str = ""
    message: str = ""
    identity: MeasurementIdentity | None = None


@dataclass(slots=True)
class BenchStatus:
    """Current bench availability exposed by the daemon."""
    identity: BenchIdentity
    state: BenchState
    active_measurement_id: str | None = None
    detail: str = ""


@dataclass(slots=True)
class BenchHealthReport:
    """Health snapshot for hardware and local tooling."""
    state: BenchState
    dut_state: DeviceState = DeviceState.ERROR
    tester_state: DeviceState = DeviceState.ERROR
    js_state: DeviceState = DeviceState.ERROR
    identity: BenchIdentity = field(default_factory=lambda: BenchIdentity(bench_id=""))
    summary: str = ""
    checks: list["BenchCheckResult"] = field(default_factory=list)
    checked_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(slots=True)
class BenchCheckResult:
    """One named health check with a concise operator-facing diagnosis."""
    name: str
    status: BenchCheckStatus
    summary: str
    detail: str = ""


def create_measurement_identity(
    run_name: str,
    *,
    created_at: datetime | None = None,
    suffix: str | None = None,
) -> MeasurementIdentity:
    """Create a readable, filesystem-safe, collision-resistant run identity."""
    readable_name = run_name.strip() or "measurement"
    timestamp = created_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("measurement identity timestamp must include a timezone")
    timestamp = timestamp.astimezone(timezone.utc)

    slug = re.sub(r"[^A-Za-z0-9]+", "-", readable_name).strip("-").lower()
    slug = slug[:80].rstrip("-") or "measurement"
    collision_suffix = suffix or secrets.token_hex(6)
    if not re.fullmatch(r"[A-Za-z0-9]+", collision_suffix):
        raise ValueError("measurement identity suffix must be alphanumeric")

    timestamp_text = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    return MeasurementIdentity(
        measurement_id=f"{slug}-{timestamp_text}-{collision_suffix.lower()}",
        run_name=readable_name,
        created_at=timestamp,
    )


def measurement_identity_to_dict(identity: MeasurementIdentity) -> dict[str, str]:
    return {
        "measurement_id": identity.measurement_id,
        "run_name": identity.run_name,
        "created_at": identity.created_at.isoformat(),
    }


def measurement_identity_from_dict(data: dict[str, Any]) -> MeasurementIdentity:
    created_at = _datetime_from_optional_string(data.get("created_at"))
    if created_at is None:
        raise ValueError("measurement identity requires a valid created_at timestamp")
    return MeasurementIdentity(
        measurement_id=str(data.get("measurement_id", "")),
        run_name=str(data.get("run_name", "")),
        created_at=created_at,
    )


def test_case_identity_to_dict(identity: TestCaseIdentity) -> dict[str, Any]:
    return {
        "job_id": identity.job_id,
        "ordinal": identity.ordinal,
        "total_jobs": identity.total_jobs,
        "feature": identity.feature,
        "config": identity.config,
        "config_hash": identity.config_hash,
        "mode": identity.mode,
        "partition": identity.partition,
    }


def test_case_identity_from_dict(data: dict[str, Any]) -> TestCaseIdentity:
    return TestCaseIdentity(
        job_id=str(data.get("job_id", "")),
        ordinal=int(data.get("ordinal", 0)),
        total_jobs=int(data.get("total_jobs", 0)),
        feature=str(data.get("feature", "")),
        config=str(data.get("config", "")),
        config_hash=str(data.get("config_hash", "")),
        mode=str(data.get("mode", "")),
        partition=str(data.get("partition", "")),
    )


def test_counts_to_dict(counts: TestCounts) -> dict[str, int]:
    return {
        "total": counts.total,
        "completed": counts.completed,
        "passed": counts.passed,
        "failed": counts.failed,
        "skipped": counts.skipped,
        "remaining": counts.remaining,
    }


def test_counts_from_dict(data: dict[str, Any]) -> TestCounts:
    counts = TestCounts(
        total=int(data.get("total", 0)),
        completed=int(data.get("completed", 0)),
        passed=int(data.get("passed", 0)),
        failed=int(data.get("failed", 0)),
        skipped=int(data.get("skipped", 0)),
    )
    if "remaining" in data and int(data["remaining"]) != counts.remaining:
        raise ValueError("remaining test count must equal total minus completed")
    return counts


def variable_filter_to_dict(variable_filter: VariableFilter) -> dict[str, str]:
    return {
        "name": variable_filter.name,
        "value": variable_filter.value,
    }


def variable_filter_from_dict(data: dict[str, Any]) -> VariableFilter:
    return VariableFilter(
        name=str(data.get("name", "")),
        value=str(data.get("value", "")),
    )


def variable_filters_to_dicts(filters: tuple[VariableFilter, ...]) -> list[dict[str, str]]:
    return [variable_filter_to_dict(item) for item in filters]


def variable_filters_from_dicts(data: Any) -> tuple[VariableFilter, ...]:
    if data is None:
        return ()
    if not isinstance(data, list):
        raise ValueError("variable_filters must be a list")
    return tuple(variable_filter_from_dict(item) for item in data)


def event_cursor_to_int(cursor: EventCursor) -> int:
    return cursor.value


def event_cursor_from_int(value: Any) -> EventCursor:
    return EventCursor(int(value))


def measurement_event_to_dict(event: MeasurementEvent) -> dict[str, Any]:
    return {
        **event.details,
        "measurement_id": event.measurement_id,
        "cursor": event_cursor_to_int(event.cursor),
        "timestamp": event.timestamp.isoformat(),
        "type": event.event_type,
    }


def measurement_event_from_dict(data: dict[str, Any]) -> MeasurementEvent:
    timestamp = _datetime_from_optional_string(data.get("timestamp"))
    if timestamp is None:
        raise ValueError("measurement event requires a valid timestamp")
    reserved = {"measurement_id", "cursor", "timestamp", "type"}
    return MeasurementEvent(
        measurement_id=str(data.get("measurement_id", "")),
        cursor=event_cursor_from_int(data.get("cursor", -1)),
        timestamp=timestamp,
        event_type=str(data.get("type", "")),
        details={key: value for key, value in data.items() if key not in reserved},
    )


def measurement_summary_to_dict(summary: MeasurementSummary) -> dict[str, Any]:
    return {
        "protocol_version": summary.protocol_version,
        "measurement_id": summary.measurement_id,
        "identity": (
            measurement_identity_to_dict(summary.identity) if summary.identity is not None else None
        ),
        "state": summary.state.value,
        "counts": test_counts_to_dict(summary.counts),
        "current_test": (
            test_case_identity_to_dict(summary.current_test)
            if summary.current_test is not None
            else None
        ),
        "failed_tests": [test_case_identity_to_dict(test) for test in summary.failed_tests],
    }


def measurement_summary_from_dict(data: dict[str, Any]) -> MeasurementSummary:
    identity_data = data.get("identity")
    current_test_data = data.get("current_test")
    return MeasurementSummary(
        protocol_version=int(data.get("protocol_version", 0)),
        measurement_id=str(data.get("measurement_id", "")),
        identity=(measurement_identity_from_dict(identity_data) if identity_data else None),
        state=MeasurementState(str(data.get("state", MeasurementState.FAILED.value))),
        counts=test_counts_from_dict(data.get("counts", {})),
        current_test=(test_case_identity_from_dict(current_test_data) if current_test_data else None),
        failed_tests=tuple(
            test_case_identity_from_dict(test) for test in data.get("failed_tests", [])
        ),
    )


def measurement_event_page_to_dict(page: MeasurementEventPage) -> dict[str, Any]:
    return {
        "protocol_version": page.protocol_version,
        "measurement_id": page.measurement_id,
        "events": [measurement_event_to_dict(event) for event in page.events],
        "next_cursor": event_cursor_to_int(page.next_cursor),
        "oldest_retained_cursor": event_cursor_to_int(page.oldest_retained_cursor),
        "truncated": page.truncated,
    }


def measurement_event_page_from_dict(data: dict[str, Any]) -> MeasurementEventPage:
    return MeasurementEventPage(
        protocol_version=int(data.get("protocol_version", 0)),
        measurement_id=str(data.get("measurement_id", "")),
        events=tuple(measurement_event_from_dict(event) for event in data.get("events", [])),
        next_cursor=event_cursor_from_int(data.get("next_cursor", 0)),
        oldest_retained_cursor=event_cursor_from_int(data.get("oldest_retained_cursor", 0)),
        truncated=bool(data.get("truncated", False)),
    )


def bench_identity_to_dict(identity: BenchIdentity) -> dict[str, str]:
    return {
        "bench_id": identity.bench_id,
        "hostname": identity.hostname,
        "board": identity.board,
        "device": identity.device,
        "profile": identity.profile,
    }


def bench_check_to_dict(check: BenchCheckResult) -> dict[str, str]:
    result = {
        "name": check.name,
        "status": check.status.value,
        "summary": check.summary,
    }
    if check.detail:
        result["detail"] = check.detail
    return result


def bench_health_report_to_dict(report: BenchHealthReport) -> dict[str, Any]:
    return {
        "identity": bench_identity_to_dict(report.identity),
        "state": report.state.value,
        "dut_state": report.dut_state.value,
        "tester_state": report.tester_state.value,
        "js_state": report.js_state.value,
        "summary": report.summary,
        "checks": [bench_check_to_dict(check) for check in report.checks],
        "checked_at": report.checked_at.isoformat(),
    }


def bench_identity_from_dict(data: dict[str, Any] | None) -> BenchIdentity:
    data = data or {}
    return BenchIdentity(
        bench_id=str(data.get("bench_id", "")),
        board=str(data.get("board", "")),
        device=str(data.get("device", "")),
        hostname=str(data.get("hostname", "")),
        profile=str(data.get("profile", "bench-lite")),
    )


def bench_check_from_dict(data: dict[str, Any]) -> BenchCheckResult:
    return BenchCheckResult(
        name=str(data.get("name", "")),
        status=BenchCheckStatus(str(data.get("status", BenchCheckStatus.FAIL.value))),
        summary=str(data.get("summary", "")),
        detail=str(data.get("detail", "")),
    )


def bench_health_report_from_dict(data: dict[str, Any]) -> BenchHealthReport:
    checked_at = data.get("checked_at")
    if isinstance(checked_at, str):
        try:
            parsed_checked_at = datetime.fromisoformat(checked_at)
        except ValueError:
            parsed_checked_at = datetime.utcnow()
    else:
        parsed_checked_at = datetime.utcnow()

    return BenchHealthReport(
        identity=bench_identity_from_dict(data.get("identity")),
        state=BenchState(str(data.get("state", BenchState.ERROR.value))),
        dut_state=DeviceState(str(data.get("dut_state", DeviceState.ERROR.value))),
        tester_state=DeviceState(str(data.get("tester_state", DeviceState.ERROR.value))),
        js_state=DeviceState(str(data.get("js_state", DeviceState.ERROR.value))),
        summary=str(data.get("summary", "")),
        checks=[bench_check_from_dict(check) for check in data.get("checks", [])],
        checked_at=parsed_checked_at,
    )


def firmware_image_to_dict(image: FirmwareImage | None) -> dict[str, str] | None:
    if image is None:
        return None
    return {"name": image.name, "url": image.url, "sha256": image.sha256}


def firmware_image_from_dict(data: dict[str, Any] | None) -> FirmwareImage | None:
    if not data:
        return None
    return FirmwareImage(
        name=str(data.get("name", "")),
        url=str(data.get("url", "")),
        sha256=str(data.get("sha256", "")),
    )


def firmware_data_to_dict(firmware: FirmwareData) -> dict[str, Any]:
    return {
        "dut_img": firmware_image_to_dict(firmware.dut_img),
        "tester_img": firmware_image_to_dict(firmware.tester_img),
        "board": firmware.board,
        "device": firmware.device,
        "dut_img_hash": firmware.dut_img_hash,
        "tester_img_hash": firmware.tester_img_hash,
        "bootloader_img": firmware_image_to_dict(firmware.bootloader_img),
        "bootloader_img_hash": firmware.bootloader_img_hash,
        "xapi_file": firmware_image_to_dict(firmware.xapi_file),
        "xapi_file_hash": firmware.xapi_file_hash,
        "git_commit": firmware.git_commit,
        "sisdk_source": firmware.sisdk_source,
        "sisdk_version": firmware.sisdk_version,
        "sisdk_commit": firmware.sisdk_commit,
        "sisdk_hash": firmware.sisdk_hash,
    }


def firmware_data_from_dict(data: dict[str, Any]) -> FirmwareData:
    dut_img = firmware_image_from_dict(data.get("dut_img"))
    tester_img = firmware_image_from_dict(data.get("tester_img"))
    if dut_img is None:
        dut_img = FirmwareImage(name=str(data.get("dut_image_name", "dut.s37")), url=str(data.get("dut_image_url", "")))
    if tester_img is None:
        tester_img = FirmwareImage(
            name=str(data.get("tester_image_name", "tester.s37")),
            url=str(data.get("tester_image_url", "")),
        )
    return FirmwareData(
        dut_img=dut_img,
        tester_img=tester_img,
        board=str(data.get("board", "")),
        device=str(data.get("device", "")),
        dut_img_hash=str(data.get("dut_img_hash", data.get("dut_image_hash", dut_img.sha256))),
        tester_img_hash=str(data.get("tester_img_hash", data.get("tester_image_hash", tester_img.sha256))),
        bootloader_img=firmware_image_from_dict(data.get("bootloader_img")),
        bootloader_img_hash=str(data.get("bootloader_img_hash", "")),
        xapi_file=firmware_image_from_dict(data.get("xapi_file")),
        xapi_file_hash=str(data.get("xapi_file_hash", "")),
        git_commit=str(data.get("git_commit", "")),
        sisdk_source=str(data.get("sisdk_source", "")),
        sisdk_version=str(data.get("sisdk_version", "")),
        sisdk_commit=str(data.get("sisdk_commit", "")),
        sisdk_hash=str(data.get("sisdk_hash", "")),
    )


def measurement_request_to_dict(request: MeasurementRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "identity": measurement_identity_to_dict(request.identity) if request.identity else None,
        "selector": {
            "mode": request.selector.mode,
            "feature": request.selector.feature,
            "config": request.selector.config,
            "config_hash": request.selector.config_hash,
            "variable_filters": variable_filters_to_dicts(request.selector.variable_filters),
        },
        "firmware": firmware_data_to_dict(request.firmware),
        "mode": request.mode.value,
        "requested_by": request.requested_by,
        "upload_results_to_database": request.upload_results_to_database,
    }


def measurement_request_from_dict(data: dict[str, Any]) -> MeasurementRequest:
    selector = data.get("selector", {})
    identity_data = data.get("identity")
    return MeasurementRequest(
        request_id=str(data.get("request_id", "")),
        selector=SuiteSelector(
            mode=str(selector.get("mode", selector.get("suite", "ble"))),
            feature=str(selector.get("feature", "")),
            config=str(selector.get("config", "default")),
            config_hash=str(selector.get("config_hash", "")),
            variable_filters=variable_filters_from_dicts(selector.get("variable_filters")),
        ),
        firmware=firmware_data_from_dict(data.get("firmware", data)),
        mode=MeasurementMode(str(data.get("mode", MeasurementMode.DEFAULT.value))),
        requested_by=str(data.get("requested_by", "")),
        upload_results_to_database=bool(data.get("upload_results_to_database", False)),
        identity=measurement_identity_from_dict(identity_data) if identity_data else None,
    )


def artifact_to_dict(artifact: Artifacts) -> dict[str, str]:
    return {
        "name": artifact.name,
        "path": str(artifact.path) if artifact.path is not None else "",
        "url": artifact.url,
        "result_type": artifact.result_type.value,
    }


def artifact_from_dict(data: dict[str, Any]) -> Artifacts:
    path = str(data.get("path", ""))
    return Artifacts(
        name=str(data.get("name", "")),
        path=Path(path) if path else None,
        url=str(data.get("url", "")),
        result_type=ResultType(str(data.get("result_type", ResultType.DIAGNOSTIC.value))),
    )


def measurement_result_to_dict(result: MeasurementResult) -> dict[str, Any]:
    return {
        "result_type": ResultType.CAMPAIGN.value,
        "measurement_id": result.measurement_id,
        "identity": measurement_identity_to_dict(result.identity) if result.identity else None,
        "state": result.state.value,
        "started_at": result.started_at.isoformat() if result.started_at else "",
        "finished_at": result.finished_at.isoformat() if result.finished_at else "",
        "artifacts": [artifact_to_dict(artifact) for artifact in result.artifacts],
        "progress_percent": result.progress_percent,
        "current_step": result.current_step,
        "message": result.message,
    }


def measurement_result_from_dict(data: dict[str, Any]) -> MeasurementResult:
    identity_data = data.get("identity")
    return MeasurementResult(
        measurement_id=str(data.get("measurement_id", "")),
        state=MeasurementState(str(data.get("state", MeasurementState.FAILED.value))),
        started_at=_datetime_from_optional_string(data.get("started_at")),
        finished_at=_datetime_from_optional_string(data.get("finished_at")),
        artifacts=[artifact_from_dict(artifact) for artifact in data.get("artifacts", [])],
        progress_percent=int(data.get("progress_percent", 0)),
        current_step=str(data.get("current_step", "")),
        message=str(data.get("message", "")),
        identity=measurement_identity_from_dict(identity_data) if identity_data else None,
    )


def _datetime_from_optional_string(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


# Backward-compatible aliases while the rest of the repo catches up.
TestState = MeasurementState
AnalysisMode = MeasurementMode
TestSelector = SuiteSelector
RunTestRequest = MeasurementRequest
TestResult = MeasurementResult

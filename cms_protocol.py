"""Shared protocol models for the CMS client, daemon, and bench layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

class DeviceState(str, Enum):
    READY = "ready"
    ERROR = "error"

class BenchState(str, Enum):
    """Runtime state of one physical bench."""
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"
    ERROR = "error"


class MeasurementState(str, Enum):
    """Lifecycle states visible to the remote client."""
    ACCEPTED = "accepted"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class MeasurementMode(str, Enum):
    """High-level measurement profile requested by the client."""
    DEBUG = "debug"
    DEFAULT = "default"
    FULL_SWEEP = "full_sweep"
    DIFF = "diff"


@dataclass(slots=True)
class BenchIdentity:
    """Stable metadata that identifies one CMS bench."""
    bench_id: str
    board: str = ""
    device: str = ""
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
    dut_img_hash: str = ""
    tester_img_hash: str = ""
    bootloader_img: FirmwareImage | None = None
    bootloader_img_hash: str = ""
    git_commit: str = ""
    sisdk_version: str = ""
    sisdk_commit: str = ""


@dataclass(slots=True)
class SuiteSelector:
    """Select the suite, feature, and config variant to execute on the bench."""
    mode: str
    feature: str
    config: str = "default"


@dataclass(slots=True)
class MeasurementRequest:
    """One remote measurement request submitted by the client."""
    request_id: str
    selector: SuiteSelector
    firmware: FirmwareData
    mode: MeasurementMode = MeasurementMode.DEFAULT
    requested_by: str = ""


@dataclass(slots=True)
class Artifacts:
    """Artifact metadata returned once a measurement completes."""
    name: str
    path: Path | None = None
    url: str = ""


@dataclass(slots=True)
class MeasurementResult:
    """Structured measurement outcome returned by the bench layer."""
    measurement_id: str
    state: MeasurementState
    started_at: datetime | None = None
    finished_at: datetime | None = None
    artifacts: list[Artifacts] = field(default_factory=list)


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
    dut_state: DeviceState
    tester_state: DeviceState
    js_state: DeviceState


# Backward-compatible aliases while the rest of the repo catches up.
TestState = MeasurementState
AnalysisMode = MeasurementMode
TestSelector = SuiteSelector
RunTestRequest = MeasurementRequest
TestResult = MeasurementResult

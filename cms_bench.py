"""Bench-local execution primitives for the Current Measurement System.

`cms_bench.py` is intentionally transport-agnostic. It does not expose HTTP
routes or own remote client policy. Its responsibility is to control the local
bench hardware and execute one measurement run at a time when asked by
`cmsd.py`.
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass, field
from pathlib import Path

from cms_protocol import (
    BenchHealthReport,
    BenchIdentity,
    BenchState,
    FirmwareData,
    MeasurementRequest,
    MeasurementResult,
)

CMS_BENCH_CONFIG = "cms_bench_runtime.json"

@dataclass(slots=True)
class BenchResources:
    """Static hardware mapping needed to operate one physical bench."""

    dut_probe_sn: str = ""
    tester_probe_sn: str = ""
    dut_serial_port: str = ""
    tester_serial_port: str = ""
    board: str = ""
    device: str = ""
    result_db: str = ""


@dataclass(slots=True)
class BenchSession:
    """Execution context prepared by the daemon for one active measurement."""
    measurement_id: str
    runtime_dir: Path
    manifest: FirmwareData
    request: MeasurementRequest
    notes: list[str] = field(default_factory=list)


class CMSBench:
    """Own the hardware-facing steps for one current-measurement bench."""

    def __init__(self):
        self.identity = None
        self.resources = None
        self.state = BenchState.IDLE

    def initialize(self) -> None:
        """Validate that the local probes, serial ports, and tools are usable."""
        config = None
        with open(CMS_BENCH_CONFIG, "r") as f:
            config = json.load(f)

        identity = BenchIdentity(
            platform.node(),
            config.get_board(),
            config.get_device()            
        )
        
        raise NotImplementedError

    def health_check(self) -> BenchHealthReport:
        """Inspect bench health and report whether the hardware is ready."""
        raise NotImplementedError

    def inspect(self, measurement_id: str) -> None:
        """Inspect the images and measurement clearance for the client."""
        raise NotImplementedError

    def run(self, measurement_id: str) -> None:
        """Run a previously accepted measurement."""
        raise NotImplementedError

    def release(self, measurement_id: str) -> None:
        """Return the bench to idle after the active measurement completes or aborts."""
        raise NotImplementedError

    def fw_save(self, file) -> None:
        """Save the CMS Client sent FW to RAM storage."""
        raise NotImplementedError

    def fw_is_valid(self, session: BenchSession) -> bool:
        """
        Run the smoke tests on the FW so that if the check passes then 
        the tests are safe to run, send a response to the client about 
        the validity of its FW Images.
        
        Before running the smoke tests, check if all images available
        are recent, as, after the Client connected.
        """
        raise NotImplementedError

    def test_prepare(self, session: BenchSession) -> None:
        """Validate the staged firmware and map it into the local test layout."""
        raise NotImplementedError

    def test_smoke(self, session: BenchSession) -> None:
        """Run a lightweight preflight before spending time on a full run."""
        raise NotImplementedError    
    
    def measurement_run(self, session: BenchSession) -> MeasurementResult:
        """Run the requested measurement flow and return structured results."""
        raise NotImplementedError

    def collect_artifacts(self, session: BenchSession, result: MeasurementResult) -> MeasurementResult:
        """Attach local artifact metadata after the execution step finishes."""
        raise NotImplementedError

"""CMS daemon bringup for client-to-bench communication.

`cmsd.py` owns the network-facing daemon and daemon-side staging area. It does
not construct or initialize `CMSBench` during bringup; the bench can be launched
later after the client/daemon interface is proven.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cms_bench import BenchSession
from cms_protocol import (
    BenchHealthReport,
    BenchState,
    DeviceState,
    MeasurementRequest,
    MeasurementResult,
)


@dataclass(slots=True)
class DaemonConfig:
    """Configuration needed to run one daemon instance."""

    host: str = "0.0.0.0"
    port: int = 8000
    runtime_root: Path = Path("artifacts/cmsd/runtime")
    artifacts_root: Path = Path("artifacts/cmsd")
    api_token: str = ""


@dataclass(slots=True)
class DaemonFiles:
    """Daemon-owned files visible before the bench is launched."""

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


class CMSDaemon:
    """Network service that brokers one active remote measurement for one bench."""

    def __init__(self, config: DaemonConfig | None = None):
        self.config = config or DaemonConfig()
        self.active_measurement_id: str | None = None
        self.files = self._daemon_files()
        self.initialized = False
        self.startup_error = ""

    def initialize(self) -> None:
        """Prepare daemon runtime directories and staging files."""

        try:
            self.config.runtime_root.mkdir(parents=True, exist_ok=True)
            self.config.artifacts_root.mkdir(parents=True, exist_ok=True)
            self._touch_staging_files()
            self.initialized = True
            self.startup_error = ""
        except OSError as exc:
            self.initialized = False
            self.startup_error = str(exc)

    def create_runtime_workspace(self, measurement_id: str) -> Path:
        """Create the runtime workspace where incoming firmware is staged."""

        workspace = self.config.runtime_root / "measurements" / measurement_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def fw_fetch(self, request: MeasurementRequest, runtime_dir: Path) -> None:
        """Stage or fetch the requested firmware payloads into the runtime area."""
        raise NotImplementedError

    def get_health(self) -> BenchHealthReport:
        """Return daemon bringup health before the physical bench is launched."""

        if self.initialized:
            return BenchHealthReport(
                state=BenchState.OFFLINE,
                dut_state=DeviceState.ERROR,
                tester_state=DeviceState.ERROR,
                js_state=DeviceState.ERROR,
            )
        return BenchHealthReport(
            state=BenchState.ERROR,
            dut_state=DeviceState.ERROR,
            tester_state=DeviceState.ERROR,
            js_state=DeviceState.ERROR,
        )

    def get_status(self) -> dict[str, Any]:
        """Return daemon state and staged file paths."""

        return {
            "daemon_initialized": self.initialized,
            "startup_error": self.startup_error,
            "bench_launched": False,
            "active_measurement_id": self.active_measurement_id,
            "runtime_root": str(self.config.runtime_root),
            "artifacts_root": str(self.config.artifacts_root),
            "files": self.files.as_dict(),
        }

    def measurement_accept(self, request: MeasurementRequest) -> dict[str, Any]:
        """Stage the current measurement request without launching the bench."""

        self.active_measurement_id = request.request_id
        self.files.measurement_request.write_text(
            json.dumps(_measurement_request_to_dict(request), indent=2) + "\n",
            encoding="utf-8",
        )
        return self.get_status()

    def build_session(self, request: MeasurementRequest, runtime_dir: Path) -> BenchSession:
        """Assemble the bench execution context after the bench is launched."""
        raise NotImplementedError

    def publish_results(self, result: MeasurementResult) -> MeasurementResult:
        """Persist metadata and expose final artifacts to remote clients."""
        raise NotImplementedError

    def measurement_abort(self, measurement_id: str) -> MeasurementResult:
        """Attempt to stop the active measurement and release the bench cleanly."""
        raise NotImplementedError

    def create_app(self) -> Any:
        """Create the FastAPI app for daemon bringup routes."""

        from fastapi import FastAPI

        app = FastAPI(title="CMS Daemon")

        @app.get("/health")
        def health() -> dict[str, str]:
            report = self.get_health()
            return {
                "state": report.state.value,
                "dut_state": report.dut_state.value,
                "tester_state": report.tester_state.value,
                "js_state": report.js_state.value,
            }

        @app.get("/bench")
        def daemon_status() -> dict[str, Any]:
            return self.get_status()

        @app.get("/result_artifacts")
        def artifacts() -> dict[str, str]:
            return self.files.as_dict()

        @app.post("/measurement")
        def request_measurement(request: dict[str, Any]) -> dict[str, Any]:
            self.active_measurement_id = str(request.get("request_id", ""))
            self.files.measurement_request.write_text(
                json.dumps(request, indent=2) + "\n",
                encoding="utf-8",
            )
            return self.get_status()

        return app

    def start_service(self) -> None:
        """Start the HTTP API that exposes daemon bringup state."""

        import uvicorn

        self.initialize()
        uvicorn.run(self.create_app(), host=self.config.host, port=self.config.port)

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

    def _touch_staging_files(self) -> None:
        for path in self.files.as_dict().values():
            file_path = Path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.touch(exist_ok=True)

def _measurement_request_to_dict(request: MeasurementRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "selector": {
            "mode": request.selector.mode,
            "feature": request.selector.feature,
            "config": request.selector.config,
        },
        "mode": request.mode.value,
        "requested_by": request.requested_by,
    }

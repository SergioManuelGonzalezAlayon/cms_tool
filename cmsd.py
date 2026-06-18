"""CMS daemon bringup for client-to-bench communication.

`cmsd.py` owns the network-facing daemon and daemon-side staging area. The
daemon initializes one local `CMSBench`, exposes health and status over HTTP,
and keeps request/runtime files inside daemon-owned directories.
"""

from __future__ import annotations

import base64
import json
import shutil
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cms_bench import BenchSession, CMSBench, CMS_BENCH_CONFIG, load_bench_runtime_config
from cms_protocol import (
    Artifacts,
    BenchCheckResult,
    BenchCheckStatus,
    BenchHealthReport,
    BenchState,
    MeasurementState,
    MeasurementRequest,
    MeasurementResult,
    bench_health_report_to_dict,
    bench_identity_to_dict,
    measurement_request_from_dict,
    measurement_request_to_dict,
    measurement_result_to_dict,
)


@dataclass(slots=True)
class DaemonConfig:
    """Configuration needed to run one daemon instance."""

    host: str = "0.0.0.0"
    port: int = 8000
    runtime_root: Path = Path("artifacts/cmsd/runtime")
    artifacts_root: Path = Path("artifacts/cmsd")
    bench_config: Path = Path(CMS_BENCH_CONFIG)
    api_token: str = ""


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
        self.last_measurement_id: str | None = None
        self.cancelled_measurements: set[str] = set()

    def initialize(self) -> None:
        """Prepare daemon runtime directories, staging files, and local bench."""

        try:
            self.config.runtime_root.mkdir(parents=True, exist_ok=True)
            self.config.artifacts_root.mkdir(parents=True, exist_ok=True)
            self._touch_staging_files()

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

        workspace = self.config.runtime_root / "measurements" / measurement_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def fw_fetch(self, request: MeasurementRequest, runtime_dir: Path) -> None:
        """Stage or fetch the requested firmware payloads into the runtime area."""
        _stage_firmware_image(request.firmware.dut_img.url, runtime_dir / request.firmware.dut_img.name)
        _stage_firmware_image(request.firmware.tester_img.url, runtime_dir / request.firmware.tester_img.name)
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

    def measurement_accept(self, request: MeasurementRequest) -> dict[str, Any]:
        """Stage the current measurement request without launching execution."""

        self.active_measurement_id = request.request_id
        self.measurements[request.request_id] = MeasurementResult(
            measurement_id=request.request_id,
            state=MeasurementState.ACCEPTED,
            started_at=datetime.utcnow(),
            progress_percent=5,
            current_step=MeasurementState.ACCEPTED.value,
            message="measurement accepted",
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

        self.measurement_accept(request)
        runtime_dir = self.create_runtime_workspace(request.request_id)
        try:
            self._update_measurement(
                request.request_id,
                MeasurementState.WAITING_FOR_ARTIFACTS,
                20,
                "staging firmware artifacts",
            )
            if image_payloads:
                _write_firmware_payloads(request, runtime_dir, image_payloads)
            else:
                self.fw_fetch(request, runtime_dir)
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
        return result

    def run_staged_test(
        self,
        *,
        mode: str,
        feature: str,
        config: str = "default",
        request_id: str = "",
        upload_results_to_database: bool = False,
    ) -> MeasurementResult:
        """Run a test using the most recently staged firmware request."""

        if self.last_firmware_request is None:
            return MeasurementResult(
                measurement_id=request_id,
                state=MeasurementState.FAILED,
                finished_at=datetime.utcnow(),
                progress_percent=100,
                current_step=MeasurementState.FAILED.value,
                message="no firmware has been staged; run SEND_FW first",
            )
        request = measurement_request_from_dict(measurement_request_to_dict(self.last_firmware_request))
        request.request_id = request_id or f"{request.request_id}-test"
        request.mode = request.mode.__class__(mode)
        request.selector.mode = "ble"
        request.selector.feature = feature
        request.selector.config = config
        request.upload_results_to_database = upload_results_to_database
        result = self.execute_measurement(request)
        self.last_measurement_id = result.measurement_id
        return result

    def start_staged_test(
        self,
        *,
        mode: str,
        feature: str,
        config: str = "default",
        request_id: str = "",
        upload_results_to_database: bool = False,
    ) -> MeasurementResult:
        """Start a test in the background using the most recently staged firmware."""

        if self.last_firmware_request is None:
            return MeasurementResult(
                measurement_id=request_id,
                state=MeasurementState.FAILED,
                finished_at=datetime.utcnow(),
                progress_percent=100,
                current_step=MeasurementState.FAILED.value,
                message="no firmware has been staged; run SEND_FW first",
            )

        request = measurement_request_from_dict(measurement_request_to_dict(self.last_firmware_request))
        request.request_id = request_id or f"{request.request_id}-test"
        request.mode = request.mode.__class__(mode)
        request.selector.mode = "ble"
        request.selector.feature = feature
        request.selector.config = config
        request.upload_results_to_database = upload_results_to_database

        started = MeasurementResult(
            measurement_id=request.request_id,
            state=MeasurementState.ACCEPTED,
            started_at=datetime.utcnow(),
            progress_percent=1,
            current_step=MeasurementState.ACCEPTED.value,
            message="test accepted",
        )
        self.measurements[request.request_id] = started
        self.active_measurement_id = request.request_id
        thread = threading.Thread(
            target=self.execute_measurement,
            args=(request,),
            kwargs={"staged_runtime_dir": self.last_firmware_runtime_dir},
            daemon=True,
        )
        thread.start()
        return started

    def build_session(self, request: MeasurementRequest, runtime_dir: Path) -> BenchSession:
        """Assemble the bench execution context after the bench is launched."""
        return BenchSession(
            measurement_id=request.request_id,
            runtime_dir=runtime_dir,
            manifest=request.firmware,
            request=request,
        )

    def publish_results(self, result: MeasurementResult) -> MeasurementResult:
        """Persist metadata and expose final artifacts to remote clients."""
        published_dir = self.config.artifacts_root / "measurements" / result.measurement_id
        published_dir.mkdir(parents=True, exist_ok=True)

        published_artifacts = []
        for artifact in result.artifacts:
            if artifact.path is None or not artifact.path.exists():
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
                )
            )
        self.measurements[result.measurement_id] = result
        self.last_measurement_id = result.measurement_id
        if self.active_measurement_id == result.measurement_id:
            self.active_measurement_id = None
        return result

    def measurement_abort(self, measurement_id: str) -> MeasurementResult:
        """Attempt to stop the active measurement and release the bench cleanly."""
        result = self.measurements.get(measurement_id)
        if result is None:
            result = MeasurementResult(measurement_id=measurement_id, state=MeasurementState.CANCELLED)
        result.state = MeasurementState.CANCELLED
        result.finished_at = datetime.utcnow()
        result.progress_percent = 100
        result.current_step = MeasurementState.CANCELLED.value
        result.message = "measurement cancelled"
        self.measurements[measurement_id] = result
        self.cancelled_measurements.add(measurement_id)
        if self.active_measurement_id == measurement_id:
            self.active_measurement_id = None
        return result

    def execute_measurement(
        self,
        request: MeasurementRequest,
        staged_runtime_dir: Path | None = None,
    ) -> MeasurementResult:
        """Run one V1 synchronous measurement path."""

        self.measurement_accept(request)
        runtime_dir = self.create_runtime_workspace(request.request_id)
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
            )
        return self.publish_results(result)

    def create_app(self) -> Any:
        """Create the FastAPI app for daemon bringup routes."""

        from fastapi import FastAPI
        from fastapi.responses import FileResponse

        app = FastAPI(title="CMS Daemon")

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
                request_id=str(request.get("request_id", "")),
                upload_results_to_database=bool(request.get("upload_results_to_database", False)),
            )
            return measurement_result_to_dict(result)

        @app.get("/measurement/{measurement_id}")
        def measurement_status(measurement_id: str) -> dict[str, Any]:
            result = self.measurements.get(measurement_id)
            if result is None:
                result_path = self.config.artifacts_root / "measurements" / measurement_id / "measurement_result.json"
                if result_path.exists():
                    return json.loads(result_path.read_text(encoding="utf-8"))
                return measurement_result_to_dict(
                    MeasurementResult(
                        measurement_id=measurement_id,
                        state=MeasurementState.FAILED,
                        progress_percent=100,
                        current_step=MeasurementState.FAILED.value,
                        message="measurement id is unknown",
                    )
                )
            return measurement_result_to_dict(result)

        @app.get("/measurement/{measurement_id}/artifacts")
        def measurement_artifacts(measurement_id: str) -> dict[str, Any]:
            result = self.measurements.get(measurement_id)
            if result is None:
                result_path = self.config.artifacts_root / "measurements" / measurement_id / "measurement_result.json"
                if result_path.exists():
                    result = MeasurementResult(
                        measurement_id=measurement_id,
                        state=MeasurementState.PASSED,
                    )
                    payload = json.loads(result_path.read_text(encoding="utf-8"))
                    return {"artifacts": payload.get("artifacts", [])}
                return {"artifacts": []}
            return {"artifacts": measurement_result_to_dict(result)["artifacts"]}

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
            safe_name = _safe_artifact_name(urllib.parse.unquote(artifact_path))
            artifact_path = self.config.artifacts_root / "measurements" / measurement_id / safe_name
            return FileResponse(artifact_path)

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


def _copy_staged_firmware(
    request: MeasurementRequest,
    staged_runtime_dir: Path,
    runtime_dir: Path,
) -> None:
    images = [request.firmware.dut_img, request.firmware.tester_img]
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


def _validation_failure_message(session: BenchSession) -> str:
    if session.notes:
        return session.notes[-1]
    return "firmware validation failed"


if __name__ == "__main__":
    cmsd = CMSDaemon()
    cmsd.start_service()

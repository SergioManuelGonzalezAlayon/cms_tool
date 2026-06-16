"""Bench-local execution primitives for the Current Measurement System.

`cms_bench.py` is intentionally transport-agnostic. It does not expose HTTP
routes or own remote client policy. Its responsibility is to control the local
bench hardware and execute one measurement run at a time when asked by
`cmsd.py`.
"""

from __future__ import annotations

import json
import platform
import shutil
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
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
    Artifacts,
    FirmwareData,
    MeasurementRequest,
    MeasurementResult,
    MeasurementState,
    measurement_request_to_dict,
    measurement_result_to_dict,
)

CMS_BENCH_CONFIG = "cms_bench_runtime.json"


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
    device: str = ""
    result_db: str = ""
    commander_path: str = ""
    xapi_path: str = ""
    group_id: int | None = None
    group_name: str = ""


@dataclass(slots=True)
class BenchSession:
    """Execution context prepared by the daemon for one active measurement."""

    measurement_id: str
    runtime_dir: Path
    manifest: FirmwareData
    request: MeasurementRequest
    notes: list[str] = field(default_factory=list)


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
                _tool_check("xapi", resources.xapi_path),
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
        if session.manifest.bootloader_img is not None:
            bootloader_path = session.runtime_dir / session.manifest.bootloader_img.name
            _validate_firmware_file(
                bootloader_path,
                session.manifest.bootloader_img.sha256 or session.manifest.bootloader_img_hash,
            )

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
            result_path = session.runtime_dir / "result.json"
            result = MeasurementResult(
                measurement_id=session.measurement_id,
                state=MeasurementState.PASSED,
                started_at=started_at,
                finished_at=datetime.utcnow(),
                progress_percent=90,
                current_step=MeasurementState.COLLECTING_ARTIFACTS.value,
                message="measurement placeholder completed",
            )
            result_path.write_text(
                json.dumps(
                    {
                        "measurement": measurement_result_to_dict(result),
                        "request": measurement_request_to_dict(session.request),
                        "notes": session.notes,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            return self.collect_artifacts(session, result)
        except RuntimeError as exc:
            return MeasurementResult(
                measurement_id=session.measurement_id,
                state=MeasurementState.FAILED,
                started_at=started_at,
                finished_at=datetime.utcnow(),
                progress_percent=100,
                current_step=MeasurementState.FAILED.value,
                message=str(exc),
            )
        finally:
            self.state = BenchState.IDLE

    def collect_artifacts(
        self,
        session: BenchSession,
        result: MeasurementResult,
    ) -> MeasurementResult:
        """Attach local artifact metadata after the execution step finishes."""
        for path in sorted(session.runtime_dir.glob("*")):
            if path.is_file():
                result.artifacts.append(Artifacts(name=path.name, path=path))
        result.progress_percent = 100
        if result.state is MeasurementState.PASSED:
            result.current_step = MeasurementState.PASSED.value
            result.message = "measurement artifacts collected"
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
            "ble.xapi": resources.xapi_path,
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
        device=identity.device,
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
        device=str(firmware.get("device", "")),
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
        device=str(metadata.get("cms_device", dut.get("part", {}).get("variant", ""))),
    )


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

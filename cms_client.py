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
import urllib.request

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cms_protocol import (
    BenchHealthReport,
    BenchIdentity,
    BenchState,
    BenchStatus,
    FirmwareImage,
    FirmwareData,
    MeasurementMode,
    MeasurementRequest,
    MeasurementResult,
    MeasurementState,
    SuiteSelector,
    bench_health_report_from_dict,
    measurement_request_from_dict,
    measurement_request_to_dict,
    measurement_result_from_dict,
)

CMS_CLIENT_CONFIG = "cms_client_config.json"


@dataclass(slots=True)
class CMSClientConfig:
    """Configuration needed for one remote CMS daemon client."""

    base_url: str = ""
    api_token: str = ""
    timeout_s: float = 30.0
    target: dict[str, str] | None = None


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
        request_id: str = "",
    ) -> MeasurementResult:
        """Start a remote test using the most recently staged firmware."""
        payload = self._request_json(
            "POST",
            "/test",
            {
                "mode": mode.value,
                "feature": feature,
                "config": config,
                "request_id": request_id,
            },
        )
        return measurement_result_from_dict(payload)

    def get_measurement(self, measurement_id: str) -> MeasurementResult:
        """Fetch the current state and result summary for one submitted measurement."""
        return measurement_result_from_dict(self._request_json("GET", f"/measurement/{measurement_id}"))

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
        payload = self._request_json("GET", f"/measurement/{measurement_id}/artifacts")
        artifacts = payload.get("artifacts", [])
        artifact = next((item for item in artifacts if item.get("name") == artifact_name), None)
        if artifact is None:
            raise RuntimeError(f"artifact is not available: {artifact_name}")

        out_dir.mkdir(parents=True, exist_ok=True)
        destination = out_dir / artifact_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_url = str(artifact.get("url", ""))
        source_path = str(artifact.get("path", ""))
        if source_path and Path(source_path).exists():
            shutil.copy2(source_path, destination)
            return destination
        if source_url.startswith("http://") or source_url.startswith("https://"):
            urllib.request.urlretrieve(source_url, destination)
            return destination
        if source_url.startswith("/"):
            with urllib.request.urlopen(f"{self.base_url}{source_url}", timeout=self.timeout_s) as response:
                destination.write_bytes(response.read())
            return destination
        raise RuntimeError(f"artifact has no downloadable location: {artifact_name}")

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
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"CMS daemon returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"CMS daemon is unreachable: {exc.reason}") from exc

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
        if source_url.startswith("http://") or source_url.startswith("https://"):
            urllib.request.urlretrieve(source_url, destination)
            return destination
        if source_url.startswith("/"):
            with urllib.request.urlopen(f"{self.base_url}{source_url}", timeout=self.timeout_s) as response:
                destination.write_bytes(response.read())
            return destination
        raise RuntimeError(f"artifact has no downloadable location: {name}")


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
    )


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
    send_fw.add_argument("--requested-by", default="", help="request owner")

    test = subparsers.add_parser("test", aliases=["TEST"], help="run a remote test with staged firmware")
    test.add_argument("mode", choices=[mode.value for mode in MeasurementMode])
    test.add_argument("feature")
    test.add_argument("--config-name", default="default", help="test config")
    test.add_argument("--request-id", default="", help="test run id")
    test.add_argument("--out-dir", default="artifacts", help="local artifact root")

    measurement = subparsers.add_parser("measurement", help="show measurement status")
    measurement.add_argument("measurement_id")

    cancel = subparsers.add_parser("cancel", aliases=["CANCEL"], help="cancel the active test")

    artifacts = subparsers.add_parser("artifacts", aliases=["ARTIFACTS"], help="download artifacts from the last test")
    artifacts.add_argument("--out-dir", default="artifacts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_cli_parser().parse_args(argv)
    client_config = load_client_config(args.config)
    client = CMSClient(base_url=args.base_url, api_token=args.api_token, config_path=args.config)

    if args.command == "health":
        report = client.get_health()
        print(f"{report.state.value}: {report.summary}")
        for check in report.checks:
            detail = f" ({check.detail})" if check.detail else ""
            print(f"{check.status.value} {check.name}: {check.summary}{detail}")
        return 0 if report.state is not BenchState.ERROR else 1

    if args.command == "status":
        status = client.get_status()
        print(f"{status.state.value}: {status.identity.bench_id}")
        if status.active_measurement_id:
            print(f"active_measurement_id: {status.active_measurement_id}")
        if status.detail:
            print(status.detail)
        return 0 if status.state is not BenchState.ERROR else 1

    if args.command in {"tests", "TESTS"}:
        payload = client.list_tests()
        print("modes: " + ", ".join(payload.get("modes", [])))
        for feature in payload.get("features", []):
            configs = ", ".join(feature.get("configs", []))
            print(f"{feature.get('feature', '')}: {configs}")
        return 0

    if args.command in {"build", "BUILD"}:
        return _run_local_build(args.action, args.targets)

    if args.command in {"send_fw", "SEND_FW"}:
        request = _request_from_send_fw_args(args, client_config)
        result = client.send_firmware(request)
        _print_measurement_result(result)
        return 0 if result.state is not MeasurementState.FAILED else 1

    if args.command == "measurement":
        _print_measurement_result(client.get_measurement(args.measurement_id))
        return 0

    if args.command in {"test", "TEST"}:
        result = client.run_test(
            MeasurementMode(args.mode),
            args.feature,
            config=args.config_name,
            request_id=args.request_id,
        )
        if result.measurement_id:
            result = _poll_test_progress(client, result.measurement_id)
        else:
            _print_measurement_result(result)
        if result.artifacts:
            for path in client.download_last_artifacts(Path(args.out_dir)):
                print(f"saved {path}")
        if result.state is MeasurementState.PASSED:
            return 0
        return 1

    if args.command in {"cancel", "CANCEL"}:
        _print_measurement_result(client.cancel_active_test())
        return 0

    if args.command in {"artifacts", "ARTIFACTS"}:
        for path in client.download_last_artifacts(Path(args.out_dir)):
            print(path)
        return 0

    return 2


def _request_from_send_fw_args(
    args: argparse.Namespace,
    client_config: CMSClientConfig | None = None,
) -> MeasurementRequest:
    if args.request_json:
        with Path(args.request_json).open("r", encoding="utf-8") as request_file:
            return measurement_request_from_dict(json.load(request_file))

    dev_cfg = _read_json_file(Path("dev_config.json"))
    firmware_cfg = dev_cfg.get("firmware", {})
    target_cfg = client_config.target if client_config and client_config.target else {}
    dut_image_arg = args.dut_image or str(target_cfg.get("dut_image", ""))
    tester_image_arg = args.tester_image or str(target_cfg.get("tester_image", ""))
    bootloader_image_arg = args.bootloader_image or str(target_cfg.get("bootloader_image", ""))
    dut_image = _firmware_image_from_arg(dut_image_arg, "dut", firmware_cfg)
    tester_image = _firmware_image_from_arg(tester_image_arg, "tester", firmware_cfg)
    bootloader_image = _firmware_image_from_arg(
        bootloader_image_arg,
        "bootloader",
        firmware_cfg,
    ) if bootloader_image_arg or firmware_cfg.get("bootloader_slcp") else None
    xapi_file = _xapi_file_from_config(dev_cfg, target_cfg)
    xapi_hash = _sha256_file(xapi_file)

    request_id = args.request_id or f"cms-{int(time.time())}"
    return MeasurementRequest(
        request_id=request_id,
        selector=SuiteSelector(mode="ble", feature="", config="default"),
        firmware=FirmwareData(
            dut_img=FirmwareImage(
                name=dut_image["name"],
                url=dut_image["url"],
                sha256=args.dut_sha256 or str(target_cfg.get("dut_sha256", "")),
            ),
            tester_img=FirmwareImage(
                name=tester_image["name"],
                url=tester_image["url"],
                sha256=args.tester_sha256 or str(target_cfg.get("tester_sha256", "")),
            ),
            bootloader_img=FirmwareImage(
                name=bootloader_image["name"],
                url=bootloader_image["url"],
                sha256=args.bootloader_sha256 or str(target_cfg.get("bootloader_sha256", "")),
            ) if bootloader_image is not None else None,
            bootloader_img_hash=args.bootloader_sha256 or str(target_cfg.get("bootloader_sha256", "")),
            board=args.board or str(target_cfg.get("board", firmware_cfg.get("board", ""))),
            device=args.device or str(target_cfg.get("device", firmware_cfg.get("device", ""))),
            xapi_file=FirmwareImage(
                name=xapi_file.name,
                url=str(xapi_file),
                sha256=xapi_hash,
            ),
            xapi_file_hash=xapi_hash,
            git_commit=args.git_commit,
            sisdk_version=args.sisdk_version or str(target_cfg.get("sisdk", firmware_cfg.get("sisdk", ""))),
            sisdk_commit=args.sisdk_commit or str(target_cfg.get("sisdk_commit", "")),
        ),
        requested_by=args.requested_by,
    )


def _run_local_build(action: str, targets: list[str]) -> int:
    cmd = [sys.executable, str(Path("apps") / "app_manager.py"), action, *targets]
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


def _xapi_file_from_config(dev_cfg: dict[str, Any], target_cfg: dict[str, str]) -> Path:
    value = str(target_cfg.get("xapi", "") or dev_cfg.get("ble", {}).get("xapi", "")).strip()
    if not value:
        raise RuntimeError("No sl_bt.xapi configured. Set dev_config.json ble.xapi or cms_client_config.json target.xapi.")
    path = Path(value)
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"sl_bt.xapi does not exist: {path}")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    if result.message:
        print(result.message)
    for artifact in result.artifacts:
        location = artifact.url or str(artifact.path or "")
        print(f"artifact {artifact.name}: {location}")


def _poll_test_progress(client: CMSClient, measurement_id: str, poll_interval_s: float = 1.0) -> MeasurementResult:
    terminal_states = {
        MeasurementState.PASSED,
        MeasurementState.FAILED,
        MeasurementState.CANCELLED,
        MeasurementState.REJECTED,
    }
    last_progress = None
    while True:
        result = client.get_measurement(measurement_id)
        progress_key = (result.state, result.progress_percent, result.message)
        if progress_key != last_progress:
            _print_measurement_result(result)
            last_progress = progress_key
        if result.state in terminal_states:
            return result
        time.sleep(poll_interval_s)


if __name__ == "__main__":
    sys.exit(main())

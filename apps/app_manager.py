import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sys

from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.config_loader import BUILD_CONFIG_ENV, _load_json
from utils.svd import (
    GpioPortRegisters,
    ensure_svd_source,
    parse_gpio_port_registers_from_svd_file,
    svd_device_from_target_device,
    svd_export_version_from_sisdk_version,
)
from utils.wpk import parse_board_vcom_gpio, vcom_gpio_header_path
from apps.sisdk_manager import (
    ArtifactoryError,
    DEFAULT_OUTPUT_ROOT,
    download_sdk,
)


APPS_DIR = REPO_ROOT / "apps"
DEFAULT_BUILD_ROOT = REPO_ROOT / ".fw_build"
DEFAULT_BOOTLOADER_SLCP = APPS_DIR / "bootloader-uart-bgapi.slcp"
APP_CONFIG_DIR = APPS_DIR / "configs"
SDK_BLUETOOTH_APP_ROOT = Path("bluetooth_le_app") / "example"
BUILD_METADATA_FILENAME = "build_metadata.json"
FW_DATA_FILENAME = "fw_data.json"
DIFF_FW_DATA_FILENAME = "diff_fw_data.json"
BGAPI_RUNTIME_DIRNAME = "runtime"
BGAPI_XAPI_FILENAME = "sl_bt.xapi"
TOOL_ENV_VARS = {
    "slt": "SLT_PATH",
    "slc": "SLC_PATH",
    "cmake": "CMAKE_PATH",
    "commander": "COMMANDER_PATH",
}
SL_BT_XAPI_PATH = Path("bluetooth_le_host") / "api" / "sl_bt.xapi"
SLC_SDK_PACKAGE_PATH_FLAG = "--sdk-package-path"
FIRMWARE_IMAGE_SUFFIXES = (".s37", ".hex", ".bin")
CMS_FLASHABLE_IMAGE_SUFFIXES = (".s37", ".hex")
FIRMWARE_ARTIFACT_SUFFIXES = FIRMWARE_IMAGE_SUFFIXES + (".map", ".out")
FIRMWARE_ROLE_BY_PROJECT = {
    "bootloader-uart-bgapi": "bootloader",
    "ncp_ble_dut": "dut_no_dcdc",
    "ncp_ble_dut_dcdc": "dut_dcdc",
    "ncp_ble_tester": "tester",
}
APP_PROFILE_PROJECT_NAMES = {
    "dut_no_dcdc": "ncp_ble_dut",
    "dut_dcdc": "ncp_ble_dut_dcdc",
    "tester": "ncp_ble_tester",
}
APP_PROFILE_BY_LEGACY_PROJECT = {
    project_name: profile_name
    for profile_name, project_name in APP_PROFILE_PROJECT_NAMES.items()
}
REQUIRED_DIFF_FIRMWARE_ROLES = frozenset(FIRMWARE_ROLE_BY_PROJECT.values())


@dataclass(frozen=True, slots=True)
class SDKSelection:
    path: Path
    version: str
    source: str
    commit: str = ""
    sdk_hash: str = ""
    xapi_path: Path | None = None
    xapi_hash: str = ""
    artifactory_version: str = ""
    artifactory_build: str = ""
    archive_path: Path | None = None
    archive_sha256: str = ""


_TOOL_OVERRIDES: dict[str, str] = {}


def _resolve_executable_candidate(value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value)
    if path.is_file():
        return str(path)
    return shutil.which(value)


def resolve_tool(tool_name: str, *, role: str = "build") -> str:
    env_name = TOOL_ENV_VARS.get(tool_name, f"{tool_name.upper()}_PATH")
    env_value = os.getenv(env_name)
    resolved = _resolve_executable_candidate(env_value)
    if resolved:
        return resolved

    configured = _TOOL_OVERRIDES.get(tool_name) or read_client_config().get("tools", {}).get(tool_name)
    resolved = _resolve_executable_candidate(configured)
    if resolved:
        return resolved

    resolved = shutil.which(tool_name)
    if resolved:
        return resolved

    raise RuntimeError(
        f"Required tool '{tool_name}' not found on PATH. "
        f"Configure cms_client_config.json tools.{tool_name}, set {env_name}, "
        "or ensure the Simplicity Studio CLI tools and CMake are installed and available."
    )


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    ok_returncodes: set[int] | None = None,
    ok_result: Callable[[subprocess.CompletedProcess], bool] | None = None,
) -> subprocess.CompletedProcess:
    exe = resolve_tool(cmd[0])
    result = subprocess.run([exe, *cmd[1:]], cwd=cwd, check=False, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    accepted = {0}
    if ok_returncodes:
        accepted.update(ok_returncodes)
    if result.returncode not in accepted and not (ok_result and ok_result(result)):
        raise subprocess.CalledProcessError(result.returncode, [exe, *cmd[1:]], output=result.stdout, stderr=result.stderr)
    return result


def read_client_config(path: Path | None = None) -> dict:
    if path is None:
        configured = os.getenv(BUILD_CONFIG_ENV)
        path = Path(configured) if configured else REPO_ROOT / "cms_client_config.json"
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    if not path.exists():
        return {}
    return _load_json(path)


def effective_build_config(config: dict, overrides: argparse.Namespace | None = None) -> dict:
    """Normalize persistent client configuration and optional CLI overrides."""
    target = config.get("target", {}) if isinstance(config.get("target"), dict) else {}
    artifactory = (
        config.get("artifactory", {})
        if isinstance(config.get("artifactory"), dict)
        else {}
    )
    build = config.get("build", {}) if isinstance(config.get("build"), dict) else {}
    artifactory = dict(artifactory)
    tools = dict(config.get("tools", {})) if isinstance(config.get("tools"), dict) else {}
    use_artifactory = (
        bool(getattr(overrides, "artifactory", False))
        if overrides is not None
        else False
    )
    if overrides is not None and use_artifactory:
        if getattr(overrides, "sisdk_version", ""):
            artifactory["version"] = str(overrides.sisdk_version)
        if getattr(overrides, "build_number", ""):
            artifactory["build"] = str(overrides.build_number)

    def override(name: str, fallback=None):
        value = getattr(overrides, name, None) if overrides is not None else None
        return value if value not in (None, "") else fallback

    for tool_name in TOOL_ENV_VARS:
        value = override(tool_name, tools.get(tool_name))
        if value:
            tools[tool_name] = str(value)

    firmware = {
        "board": str(override("board", target.get("board", ""))),
        "brev": str(override("board_rev", target.get("board_rev", target.get("brev", "")))),
        "device": str(override("target_device", target.get("device", ""))),
        "sisdk": str(
            artifactory.get("version", "")
            if use_artifactory
            else override("sisdk_version", target.get("sisdk", target.get("sisdk_version", "")))
        ),
        "sisdk_package_path": str(
            ""
            if use_artifactory
            else override(
                "sdk_package_path",
                target.get("sisdk_package_path", target.get("sdk_package_path", "")),
            )
        ),
        "adapter_board": str(target.get("adapter_board", target.get("wpk_board", "brd4002a"))),
        "svd_path": str(target.get("svd_path", "")),
        "svd_cache_dir": str(target.get("svd_cache_dir", "")),
        "build_root": str(override("build_root", build.get("root", config.get("build_root", DEFAULT_BUILD_ROOT)))),
    }
    return {
        "target": dict(target),
        "artifactory": artifactory,
        "build": dict(build),
        "tools": tools,
        "firmware": firmware,
    }


def resolve_build_root(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value)
    effective_config = effective_build_config(read_client_config())
    build_root = effective_config.get("firmware", {}).get("build_root")
    if build_root:
        return Path(build_root)
    return DEFAULT_BUILD_ROOT


def validate_firmware_config(effective_config: dict) -> dict[str, str]:
    firmware = effective_config.get("firmware", {})
    if not isinstance(firmware, dict):
        firmware = {}

    resolved_board = firmware.get("board")
    device = firmware.get("device")
    sisdk = firmware.get("sisdk")
    brev = firmware.get("brev", "")

    missing: list[str] = []
    if not resolved_board:
        missing.append("target.board")
    if not device:
        missing.append("target.device")
    if not sisdk:
        missing.append("target.sisdk")

    if missing:
        missing_fields = ", ".join(missing)
        raise RuntimeError(
            "Missing required build settings in cms_client_config.json: "
            f"{missing_fields}. "
            "Set these values under the 'target' section."
        )

    return {
        "board": str(resolved_board),
        "brev": str(brev),
        "device": str(device),
        "sisdk": str(sisdk),
        "adapter_board": str(firmware.get("adapter_board", "brd4002a") or "brd4002a"),
        "svd_version": svd_export_version_from_sisdk_version(str(sisdk)),
        "svd_device": str(firmware.get("svd_device", "") or svd_device_from_target_device(str(device))),
        "svd_path": str(firmware.get("svd_path", "")),
        "svd_cache_dir": str(firmware.get("svd_cache_dir", "")),
    }


def _validate_sdk_path(path: Path, version: str) -> Path:
    path = path.expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise RuntimeError(f"SiSDK package path does not exist or is not a directory: {path}")
    installed_version = _sisdk_version_from_properties(path)
    if installed_version != version:
        raise RuntimeError(
            "Configured SiSDK version does not match the SDK package path: "
            f"cms_client_config.json target.sisdk={version}, "
            f"{path / '.properties'} version={installed_version}"
        )
    xapi_path = path / SL_BT_XAPI_PATH
    if not xapi_path.is_file():
        raise RuntimeError(f"Resolved SiSDK '{version}' cannot find `sl_bt.xapi`: {xapi_path}")
    return path


def _sisdk_version_from_properties(sdk_path: Path) -> str:
    properties_path = sdk_path / ".properties"
    if not properties_path.is_file():
        raise RuntimeError(f"SiSDK package path is missing .properties: {properties_path}")
    for line in properties_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "version" and value.strip():
            return value.strip()
    raise RuntimeError(f"SiSDK package properties are missing version: {properties_path}")


def inspect_sdk_selection(
    package_path: str | Path,
    *,
    source: str,
    include_git_commit: bool = True,
) -> SDKSelection:
    """Inspect one installed SiSDK package without relying on configured version text."""

    sdk_path = Path(package_path).expanduser().resolve()
    if not sdk_path.exists() or not sdk_path.is_dir():
        raise RuntimeError(f"SiSDK package path does not exist or is not a directory: {sdk_path}")
    version = _sisdk_version_from_properties(sdk_path)
    xapi_path = sdk_path / SL_BT_XAPI_PATH
    if not xapi_path.is_file():
        raise RuntimeError(f"Resolved SiSDK '{version}' cannot find `sl_bt.xapi`: {xapi_path}")
    commit = _sdk_git_commit(sdk_path) if include_git_commit else ""
    return SDKSelection(
        path=sdk_path,
        version=version,
        source=source,
        commit=commit,
        sdk_hash=commit,
        xapi_path=xapi_path,
        xapi_hash=_sha256(xapi_path),
    )


def sdk_selection_provenance(selection: SDKSelection) -> dict[str, str]:
    """Return the SDK facts retained for one normal or differential build variant."""

    return {
        "source": selection.source,
        "version": selection.version,
        "artifactory_version": selection.artifactory_version,
        "artifactory_build": selection.artifactory_build,
        "archive_path": str(selection.archive_path or ""),
        "archive_sha256": selection.archive_sha256,
        "package_path": str(selection.path),
        "commit": selection.commit,
        "hash": selection.sdk_hash,
        "xapi_path": str(selection.xapi_path or ""),
        "xapi_sha256": selection.xapi_hash,
    }


def resolve_diff_sdk_selections(config: dict) -> dict[str, SDKSelection]:
    """Resolve independently configured version1 and version2 SDK packages for preflight."""

    diff = config.get("diff")
    if not isinstance(diff, dict):
        raise RuntimeError(
            "cms_client_config.json must contain a diff object with "
            "sisdk_version1_path and sisdk_version2_path"
        )

    selections: dict[str, SDKSelection] = {}
    for variant in ("version1", "version2"):
        path = str(diff.get(f"sisdk_{variant}_path", "")).strip()
        if not path:
            raise RuntimeError(
                f"cms_client_config.json diff.sisdk_{variant}_path is required for cms BUILD --diff"
            )
        selections[variant] = inspect_sdk_selection(
            path,
            source=f"client_config_diff_{variant}_path",
        )
    return selections


def _sdk_git_commit(sdk_path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(sdk_path), "rev-parse", "HEAD"],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def resolve_sdk_selection(
    *,
    version: str,
    cli_package_path: str | None = None,
    config_package_path: str | None = None,
) -> SDKSelection:
    if cli_package_path:
        source = "cli_package_path"
        selection = inspect_sdk_selection(cli_package_path, source=source)
    elif config_package_path:
        source = "client_config_package_path"
        selection = inspect_sdk_selection(config_package_path, source=source)
    else:
        raise RuntimeError(
            "No SiSDK package path is configured. Set target.sisdk_package_path in "
            "cms_client_config.json to the unpacked SDK directory, or pass "
            "--sdk-package-path <path> to this build."
        )
    if selection.version != version:
        raise RuntimeError(
            "Configured SiSDK version does not match the SDK package path: "
            f"cms_client_config.json target.sisdk={version}, "
            f"{selection.path / '.properties'} version={selection.version}"
        )
    return selection


def resolve_artifactory_sdk_selection(version: str, build: str) -> SDKSelection:
    """Download and prepare one configured Artifactory SDK build in the stable cache."""

    version = version.strip()
    build = build.strip()
    if not version or not build:
        raise RuntimeError("Artifactory SDK selection requires both version and build")
    if not re.fullmatch(r"\d{4}\.\d+", version):
        raise RuntimeError("artifactory.version must use the SiSDK release format YYYY.MM")
    if not build.isdecimal():
        raise RuntimeError("artifactory.build must be a numeric Artifactory build number")

    try:
        archive = download_sdk(version, DEFAULT_OUTPUT_ROOT, build)
    except ArtifactoryError as error:
        raise RuntimeError(f"Could not obtain SiSDK {version} build {build}: {error}") from error

    package_path = archive.parent / "package"
    if not package_path.exists():
        staging_path = Path(
            tempfile.mkdtemp(prefix=".package-", dir=str(archive.parent))
        ).resolve()
        try:
            with zipfile.ZipFile(archive) as package:
                extraction_root = staging_path.resolve()
                for member in package.infolist():
                    member_path = (extraction_root / member.filename).resolve()
                    if not member_path.is_relative_to(extraction_root):
                        raise RuntimeError(
                            f"SiSDK archive contains a path outside its package: {member.filename}"
                        )

                members = package.infolist()
                total_bytes = sum(member.file_size for member in members if not member.is_dir())
                with tqdm(
                    total=total_bytes,
                    desc=f"Extracting SiSDK {version} build {build}",
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                ) as progress:
                    for member in members:
                        destination = staging_path / member.filename
                        if member.is_dir():
                            destination.mkdir(parents=True, exist_ok=True)
                            continue

                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with package.open(member) as source, destination.open("wb") as output:
                            while chunk := source.read(1024 * 1024):
                                output.write(chunk)
                                progress.update(len(chunk))

            staged_version = _sisdk_version_from_properties(staging_path)
            if staged_version != version and not staged_version.startswith(f"{version}."):
                raise RuntimeError(
                    "Downloaded SiSDK package version does not match the Artifactory release: "
                    f"requested={version}, archive={staged_version}"
                )
            if not (staging_path / SL_BT_XAPI_PATH).is_file():
                raise RuntimeError(
                    f"Downloaded SiSDK '{version}' cannot find `sl_bt.xapi` under {staging_path}"
                )
            staging_path.replace(package_path)
        finally:
            if staging_path.exists():
                shutil.rmtree(staging_path)

    selection = inspect_sdk_selection(
        package_path,
        source="artifactory",
        include_git_commit=False,
    )
    if selection.version != version and not selection.version.startswith(f"{version}."):
        raise RuntimeError(
            "Cached SiSDK package version does not match the Artifactory release: "
            f"requested={version}, cache={selection.version}"
        )
    return SDKSelection(
        path=selection.path,
        version=selection.version,
        source=selection.source,
        commit=selection.commit,
        sdk_hash=selection.sdk_hash,
        xapi_path=selection.xapi_path,
        xapi_hash=selection.xapi_hash,
        artifactory_version=version,
        artifactory_build=build,
        archive_path=archive.resolve(),
        archive_sha256=_sha256(archive),
    )


def trust_sisdk(sdk_path: Path) -> None:
    run(["slc", "signature", "trust", "-s", str(sdk_path)], cwd=REPO_ROOT)


def resolve_bootloader_slcp() -> Path:
    path = DEFAULT_BOOTLOADER_SLCP
    if not path.exists():
        raise FileNotFoundError(f"Could not resolve bootloader SLCP: {path}")
    return path


def discover_slcps(targets: list[str]) -> list[Path]:
    if not targets:
        return sorted(APPS_DIR.rglob("*.slcp"))

    slcps: list[Path] = []
    for target in targets:
        target_path = Path(target)
        if not target_path.is_absolute():
            target_path = (REPO_ROOT / target).resolve()
        if target_path.is_dir():
            slcps.extend(sorted(target_path.rglob("*.slcp")))
        elif target_path.suffix == ".slcp" and target_path.exists():
            slcps.append(target_path)
        else:
            raise FileNotFoundError(f"Could not resolve app target: {target}")
    return sorted({path.resolve() for path in slcps})


def output_dir_for(slcp: Path, build_root: Path) -> Path:
    return build_root / slcp.stem


def generate_slcp(
    slcp: Path,
    *,
    out_dir: Path,
    board: str,
    sdk_path: Path,
    project_name: str = "",
    slc_options: list[str] | tuple[str, ...] = (),
    configuration: dict | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "slc",
        "generate",
        str(slcp),
        "--sdk-package-path",
        str(sdk_path),
        "-d",
        str(out_dir),
        f"-name={project_name or slcp.stem}",
        "--with",
        board,
    ]
    cmd.extend(slc_options)
    configuration = configuration or {}
    if configuration:
        configuration_overrides = ",".join(
            f"{name}:{str(value).lower() if isinstance(value, bool) else value}"
            for name, value in configuration.items()
        )
        cmd.append(f"--configuration={configuration_overrides}")
    cmd.append("--generator-timeout=60000")
    run(cmd, cwd=REPO_ROOT)


def sisdk_release_from_version(version: str) -> str:
    match = re.fullmatch(r"(\d{4}\.\d+)(?:\.\d+)?", version.strip())
    if not match:
        raise RuntimeError(f"Cannot select an app config from SiSDK version: {version}")
    return match.group(1)


def load_app_configuration(version: str, board: str) -> tuple[dict, dict, Path]:
    release = sisdk_release_from_version(version)
    config_path = APP_CONFIG_DIR / f"sisdk{release}.json"
    if not config_path.is_file():
        raise RuntimeError(
            f"No app configuration exists for SiSDK {release}: {config_path}. "
            "Add the matching apps/configs/sisdk<version>.json before generating the project."
        )
    try:
        app_config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid SiSDK app configuration JSON: {config_path}: {error}") from error
    if not isinstance(app_config, dict):
        raise RuntimeError(f"SiSDK app configuration must be an object: {config_path}")

    application = app_config.get("application")
    if not isinstance(application, str) or not application.strip() or Path(application).name != application:
        raise RuntimeError(f"SiSDK app configuration has an invalid application name: {config_path}")

    boards = app_config.get("boards")
    if not isinstance(boards, dict):
        raise RuntimeError(f"SiSDK app configuration must contain a boards object: {config_path}")
    board_id = board.strip().lower().split("_", 1)[0]
    board_config = boards.get(board_id)
    if not isinstance(board_config, dict):
        supported = ", ".join(sorted(str(item) for item in boards)) or "none"
        raise RuntimeError(
            f"Board {board!r} is not supported by SiSDK {release} app configuration. "
            f"Supported boards: {supported}"
        )
    if not isinstance(board_config.get("prs_configuration"), str) or not board_config["prs_configuration"].strip():
        raise RuntimeError(f"Board {board_id} has no prs_configuration in {config_path}")
    profiles = board_config.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise RuntimeError(f"Board {board_id} has no device profiles in {config_path}")
    unknown_profiles = sorted(set(profiles).difference(APP_PROFILE_PROJECT_NAMES))
    if unknown_profiles:
        raise RuntimeError(
            f"SiSDK app configuration contains unsupported device profiles: {', '.join(unknown_profiles)}"
        )
    return app_config, board_config, config_path


def resolve_sdk_app_slcp(sdk_path: Path, application: str) -> Path:
    slcp = sdk_path / SDK_BLUETOOTH_APP_ROOT / application / f"{application}.slcp"
    if not slcp.is_file():
        raise FileNotFoundError(
            f"SiSDK application {application!r} was not found at the expected path: {slcp}"
        )
    return slcp


def select_app_build_targets(
    targets: list[str],
    *,
    application: str,
    profiles: dict,
    requested_device: str = "",
) -> tuple[list[str], bool]:
    profile_names = list(profiles)
    if requested_device and requested_device not in profiles:
        raise RuntimeError(
            f"Unknown --device profile {requested_device!r}. "
            f"Choose one of: {', '.join(profile_names)}"
        )

    selected_profiles: list[str] = []
    include_bootloader = not targets
    if not targets:
        selected_profiles = [requested_device] if requested_device else profile_names
        return selected_profiles, include_bootloader

    all_profiles_requested = False
    for target in targets:
        target_path = Path(target)
        if not target_path.is_absolute():
            target_path = REPO_ROOT / target_path
        if target_path.is_dir() and target_path.resolve() == APPS_DIR.resolve():
            all_profiles_requested = True
            include_bootloader = True
            continue

        target_name = Path(target).stem.lower()
        if target_name == "bootloader-uart-bgapi":
            include_bootloader = True
        elif target_name in profiles:
            selected_profiles.append(target_name)
        elif target_name in APP_PROFILE_BY_LEGACY_PROJECT:
            selected_profiles.append(APP_PROFILE_BY_LEGACY_PROJECT[target_name])
        elif target_name == application.lower():
            all_profiles_requested = True
        else:
            raise RuntimeError(
                f"Unsupported app build target {target!r}. Use {application}, a configured "
                "device profile, or bootloader-uart-bgapi.slcp."
            )

    if all_profiles_requested:
        selected_profiles.extend(profile_names)
    if requested_device:
        if selected_profiles and requested_device not in selected_profiles:
            raise RuntimeError(
                f"Target selection does not include --device profile {requested_device!r}"
            )
        selected_profiles = [requested_device]
    selected_profiles = list(dict.fromkeys(selected_profiles))
    if not selected_profiles and not include_bootloader:
        raise RuntimeError("No app or bootloader build target was selected")
    return selected_profiles, include_bootloader


def merge_app_configuration(app_config: dict, profile_config: dict) -> dict:
    common = app_config.get("configuration", {})
    profile = profile_config.get("configuration", {})
    if not isinstance(common, dict) or not isinstance(profile, dict):
        raise RuntimeError("Application and device profile configuration values must be objects")
    return {**common, **profile}


def merge_slc_options(board_config: dict, profile_config: dict) -> list[str]:
    options: list[str] = []
    for scope, config in (("board", board_config), ("device profile", profile_config)):
        values = config.get("slc_options", [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise RuntimeError(f"{scope} slc_options must be an array of strings")
        options.extend(values)
    return options


def _is_noop_upgrade_result(result: subprocess.CompletedProcess) -> bool:
    if result.returncode not in {19, -14, 4294967282}:
        return False
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).lower()
    markers = (
        "nothing to upgrade",
        "an upgrade was not performed",
        "project framework id and version are identical to target",
    )
    return any(marker in output for marker in markers)


def upgrade_slcp(slcp: Path, *, out_dir: Path) -> Path:
    generated_slcp = out_dir / slcp.name
    if not generated_slcp.exists():
        raise FileNotFoundError(
            f"Expected generated project file before upgrade: {generated_slcp}. "
            "Run generation first so upgrade operates on the generated project."
        )
    # Some SLC versions return a non-zero code when the project is already current.
    run(
        ["slc", "upgrade", "-p", str(generated_slcp.resolve())],
        cwd=REPO_ROOT,
        ok_result=_is_noop_upgrade_result,
    )
    return generated_slcp


def _relative_to_repo(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path.resolve())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_artifacts(out_dir: Path, suffixes: tuple[str, ...]) -> list[Path]:
    root = out_dir / "cmake_gcc"
    artifacts: list[Path] = []
    for suffix in suffixes:
        artifacts.extend(sorted(path.resolve() for path in root.rglob(f"*{suffix}")))
    return list(dict.fromkeys(artifacts))


def copy_bgapi_xapi(build_root: Path, sdk_path: Path) -> Path:
    runtime_dir = build_root / BGAPI_RUNTIME_DIRNAME
    runtime_dir.mkdir(parents=True, exist_ok=True)
    source = sdk_path / SL_BT_XAPI_PATH
    target = runtime_dir / BGAPI_XAPI_FILENAME
    shutil.copy2(source, target)
    return target


def resolve_vcom_build_metadata(
    *,
    firmware_cfg: dict[str, str],
    sdk_path: Path,
    build_root: Path,
) -> dict:
    """Resolve VCOM control metadata from build-time board and SVD inputs."""

    adapter_board = firmware_cfg.get("adapter_board", "brd4002a") or "brd4002a"
    header_path = vcom_gpio_header_path(
        sdk_path,
        firmware_cfg["board"],
        adapter_board,
    )
    try:
        vcom_gpio = parse_board_vcom_gpio(
            sdk_path,
            firmware_cfg["board"],
            adapter_board,
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "Unable to resolve VCOM GPIO from the selected SiSDK board header: "
            f"{header_path}. "
            "Check target.board, target.adapter_board, and target.sisdk_package_path."
        ) from exc

    cache_dir = Path(firmware_cfg.get("svd_cache_dir", "") or build_root / "svd_cache")
    configured_svd_path = firmware_cfg.get("svd_path", "")
    svd_version = firmware_cfg.get("svd_version", "") or svd_export_version_from_sisdk_version(
        firmware_cfg["sisdk"]
    )
    svd_device = firmware_cfg.get("svd_device", "") or svd_device_from_target_device(
        firmware_cfg["device"]
    )
    try:
        svd_source = ensure_svd_source(
            svd_version=svd_version,
            svd_device=svd_device,
            cache_dir=cache_dir,
            configured_svd_path=configured_svd_path,
        )
    except OSError as exc:
        raise RuntimeError(
            "Unable to use configured SVD path. "
            "Check cms_client_config.json target.svd_path."
        ) from exc

    if svd_source.path is None:
        raise RuntimeError("resolved SVD source did not provide a local file path")

    try:
        gpio_registers = parse_gpio_port_registers_from_svd_file(
            svd_source.path,
            vcom_gpio.port,
            vcom_gpio.pin,
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "Unable to resolve VCOM GPIO register addresses from SVD: "
            f"{svd_source.path}."
        ) from exc

    return _vcom_metadata_to_dict(
        board=firmware_cfg["board"],
        adapter_board=adapter_board,
        device=firmware_cfg["device"],
        svd_version=svd_source.svd_version,
        svd_device=svd_source.svd_device,
        header_path=header_path,
        svd_path=svd_source.path,
        svd_source=svd_source.source,
        artifactory_url=svd_source.artifactory_url,
        cache_path=svd_source.cache_path,
        gpio_port=vcom_gpio.port,
        gpio_pin=vcom_gpio.pin,
        gpio_registers=gpio_registers,
    )


def _vcom_metadata_to_dict(
    *,
    board: str,
    adapter_board: str,
    device: str,
    svd_version: str,
    svd_device: str,
    header_path: Path,
    svd_path: Path,
    svd_source: str,
    artifactory_url: str,
    cache_path: Path,
    gpio_port: str,
    gpio_pin: int,
    gpio_registers: GpioPortRegisters,
) -> dict:
    gpio = {
        "port": gpio_port,
        "pin": gpio_pin,
        "peripheral": gpio_registers.peripheral,
        "peripheral_base_address": _hex_u32(gpio_registers.peripheral_base_address),
        "port_base_address": _hex_u32(gpio_registers.port_base_address),
        "dout_address": _hex_u32_or_none(gpio_registers.dout_address),
        "pin_mask": _hex_u32(gpio_registers.pin_mask),
        "mode_register_address": _hex_u32_or_none(gpio_registers.mode_register_address),
        "mode_mask": _hex_u32(gpio_registers.mode_mask),
        "mode_push_pull_value": _hex_u32(gpio_registers.mode_push_pull_value),
    }
    if gpio_registers.dout_set_address is not None:
        gpio["dout_set_address"] = _hex_u32(gpio_registers.dout_set_address)
    if gpio_registers.dout_clear_address is not None:
        gpio["dout_clear_address"] = _hex_u32(gpio_registers.dout_clear_address)

    return {
        "enabled": True,
        "source": {
            "board_config_header": str(header_path.resolve()),
            "board_config_header_sha256": _sha256(header_path),
            "svd": str(svd_path.resolve()),
            "svd_sha256": _sha256(svd_path),
            "svd_source": svd_source,
            "svd_artifactory_url": artifactory_url,
            "svd_cache_path": str(cache_path),
        },
        "board": board,
        "adapter_board": adapter_board,
        "device": device,
        "svd_version": svd_version,
        "svd_device": svd_device,
        "gpio": gpio,
    }


def _hex_u32(value: int) -> str:
    return f"0x{value:08X}"


def _hex_u32_or_none(value: int | None) -> str | None:
    return None if value is None else _hex_u32(value)


def _relative_to_build_root(path: Path, build_root: Path) -> str:
    return str(path.resolve().relative_to(build_root.resolve()))


def build_slcp(*, out_dir: Path) -> Path:
    run(["cmake", "--workflow", "--preset", "project"], cwd=out_dir / "cmake_gcc")
    candidates = find_artifacts(out_dir, FIRMWARE_IMAGE_SUFFIXES)
    if not candidates:
        raise RuntimeError(f"No firmware image found under {out_dir}")
    return candidates[0]


def find_built_image(out_dir: Path) -> Path | None:
    candidates = find_artifacts(out_dir, FIRMWARE_IMAGE_SUFFIXES)
    if not candidates:
        return None
    return candidates[0]


def build_project(slcp: Path, *, out_dir: Path, board: str, sdk_path: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    image = find_built_image(out_dir)
    if image is not None:
        return image

    generated_slcp = out_dir / slcp.name
    if not generated_slcp.exists():
        generate_slcp(slcp, out_dir=out_dir, board=board, sdk_path=sdk_path)
    return build_slcp(out_dir=out_dir)


def write_build_metadata(
    *,
    slcp: Path,
    out_dir: Path,
    image: Path,
    firmware_cfg: dict[str, str],
    sdk_path: Path,
    runtime_xapi_path: Path | None,
    sdk_selection: SDKSelection | None = None,
    vcom_metadata: dict | None = None,
    app_configuration: dict | None = None,
    project_name: str = "",
) -> Path:
    artifacts = [
        {
            "name": path.name,
            "path": _relative_to_repo(path),
            "suffix": path.suffix.lower(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in find_artifacts(out_dir, FIRMWARE_ARTIFACT_SUFFIXES)
    ]
    metadata = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "project_name": project_name or slcp.stem,
        "slcp": _relative_to_repo(slcp),
        "generated_slcp": _relative_to_repo(out_dir / slcp.name),
        "firmware": {
            "board": firmware_cfg["board"],
            "brev": firmware_cfg.get("brev", ""),
            "device": firmware_cfg["device"],
            "sisdk": firmware_cfg["sisdk"],
            "sdk_path": str(sdk_path.resolve()),
        },
        "sdk": {
            "source": sdk_selection.source if sdk_selection else "",
            "version": firmware_cfg["sisdk"],
            "artifactory_version": sdk_selection.artifactory_version if sdk_selection else "",
            "artifactory_build": sdk_selection.artifactory_build if sdk_selection else "",
            "archive_path": str(sdk_selection.archive_path or "") if sdk_selection else "",
            "archive_sha256": sdk_selection.archive_sha256 if sdk_selection else "",
            "package_path": str(sdk_path.resolve()),
            "commit": sdk_selection.commit if sdk_selection else "",
            "hash": sdk_selection.sdk_hash if sdk_selection else "",
            "xapi_sha256": (
                sdk_selection.xapi_hash
                if sdk_selection
                else (_sha256(runtime_xapi_path) if runtime_xapi_path and runtime_xapi_path.is_file() else "")
            ),
        },
        "selected_image": {
            "name": image.name,
            "path": _relative_to_repo(image),
            "suffix": image.suffix.lower(),
            "size": image.stat().st_size,
            "sha256": _sha256(image),
        },
        "artifacts": artifacts,
    }
    if vcom_metadata is not None:
        metadata["vcom"] = vcom_metadata
    if app_configuration is not None:
        metadata["app_configuration"] = app_configuration
    metadata_path = out_dir / BUILD_METADATA_FILENAME
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata_path


def write_fw_data(
    *,
    build_root: Path,
    firmware_cfg: dict[str, str],
    sdk_path: Path,
    runtime_xapi_path: Path,
    summaries: list[dict],
    sdk_selection: SDKSelection | None = None,
    vcom_metadata: dict | None = None,
) -> Path:
    artifacts = firmware_artifacts_from_summaries(summaries)
    hash_by_role = {
        "dut_app_hash": "",
        "dut_no_dcdc_app_hash": "",
        "tester_app_hash": "",
        "bootloader_app_hash": "",
    }
    hash_key_by_role = {
        "dut_dcdc": "dut_app_hash",
        "dut_no_dcdc": "dut_no_dcdc_app_hash",
        "tester": "tester_app_hash",
        "bootloader": "bootloader_app_hash",
    }
    for artifact in artifacts:
        hash_by_role[hash_key_by_role[artifact["role"]]] = artifact["sha256"]

    fw_data = {
        "firmware": {
            "board": firmware_cfg["board"],
            "brev": firmware_cfg.get("brev", ""),
            "device": firmware_cfg["device"],
            "sisdk": firmware_cfg["sisdk"],
            **hash_by_role,
            "artifacts": artifacts,
        },
        "build": {
            "root": str(build_root.resolve()),
            "xapi": str(runtime_xapi_path.resolve()),
        },
        "sdk": {
            "source": sdk_selection.source if sdk_selection else "",
            "version": firmware_cfg["sisdk"],
            "artifactory_version": sdk_selection.artifactory_version if sdk_selection else "",
            "artifactory_build": sdk_selection.artifactory_build if sdk_selection else "",
            "archive_path": str(sdk_selection.archive_path or "") if sdk_selection else "",
            "archive_sha256": sdk_selection.archive_sha256 if sdk_selection else "",
            "package_path": str(sdk_path.resolve()),
            "commit": sdk_selection.commit if sdk_selection else "",
            "hash": sdk_selection.sdk_hash if sdk_selection else "",
            "xapi_sha256": sdk_selection.xapi_hash if sdk_selection else _sha256(runtime_xapi_path),
        },
    }
    if vcom_metadata is not None:
        fw_data["vcom"] = vcom_metadata
    fw_data_path = build_root / FW_DATA_FILENAME
    fw_data_path.write_text(json.dumps(fw_data, indent=2) + "\n", encoding="utf-8")
    return fw_data_path


def firmware_artifacts_from_summaries(summaries: list[dict]) -> list[dict[str, str]]:
    """Describe known firmware roles from their declared SLCP project identity."""

    artifacts: list[dict[str, str]] = []
    for summary in summaries:
        image_path = str(summary.get("image", ""))
        project_name = str(
            summary.get("project_name") or Path(str(summary.get("slcp", ""))).stem
        ).lower()
        role = str(summary.get("firmware_role", "")) or FIRMWARE_ROLE_BY_PROJECT.get(project_name, "")
        if not role or not image_path:
            continue
        image = Path(image_path)
        artifacts.append(
            {
                "role": role,
                "project_name": project_name,
                "image": str(image),
                "sha256": _sha256(image),
            }
        )
    return artifacts


def validate_required_firmware_roles(
    summaries: list[dict],
    required_roles: frozenset[str],
) -> None:
    """Reject a differential variant that lacks exactly one required named role."""

    artifacts = firmware_artifacts_from_summaries(summaries)
    validate_firmware_artifact_roles(artifacts, required_roles)
    unsupported = [
        f"{artifact['role']}={artifact['image']}"
        for artifact in artifacts
        if Path(artifact["image"]).suffix.lower() not in CMS_FLASHABLE_IMAGE_SUFFIXES
    ]
    if unsupported:
        raise RuntimeError(
            "Differential firmware roles must use a CMS-flashable .s37 or .hex image: "
            + ", ".join(sorted(unsupported))
        )


def validate_firmware_artifact_roles(
    artifacts: list[dict[str, str]],
    required_roles: frozenset[str],
) -> None:
    """Reject missing or duplicate role labels in an already materialized artifact list."""

    roles = [artifact["role"] for artifact in artifacts]
    missing = sorted(required_roles.difference(roles))
    duplicate = sorted({role for role in roles if roles.count(role) > 1})
    if missing or duplicate:
        details: list[str] = []
        if missing:
            details.append(f"missing required roles: {', '.join(missing)}")
        if duplicate:
            details.append(f"duplicate roles: {', '.join(duplicate)}")
        raise RuntimeError("Differential firmware variant is incomplete: " + "; ".join(details))


def write_diff_fw_data(
    *,
    build_root: Path,
    parsed: argparse.Namespace,
    client_config: dict,
    selections: dict[str, SDKSelection],
) -> Path:
    """Persist the two complete isolated build manifests as one diff build record."""

    target_cfg = client_config.get("target") if isinstance(client_config.get("target"), dict) else {}
    target = {
        "board": str(target_cfg.get("board", "")),
        "board_revision": str(target_cfg.get("board_rev", target_cfg.get("brev", ""))),
        "device": str(target_cfg.get("device", "")),
    }
    if not all(target.values()):
        raise RuntimeError("cms_client_config.json target board, board_rev, and device are required for cms BUILD --diff")

    variants: dict[str, dict] = {}
    for variant in ("version1", "version2"):
        variant_root = build_root / "diff" / variant
        manifest_path = variant_root / FW_DATA_FILENAME
        if not manifest_path.is_file():
            raise RuntimeError(f"Differential {variant} build manifest is missing: {manifest_path}")
        manifest = _load_json(manifest_path)
        firmware = manifest.get("firmware") if isinstance(manifest.get("firmware"), dict) else {}
        artifacts = firmware.get("artifacts") if isinstance(firmware.get("artifacts"), list) else None
        observed_target = {
            "board": str(firmware.get("board", "")),
            "board_revision": str(firmware.get("brev", "")),
            "device": str(firmware.get("device", "")),
        }
        if observed_target != target:
            raise RuntimeError(
                f"Differential {variant} firmware target does not match the configured target: "
                f"{observed_target} != {target}"
            )
        if (
            not firmware
            or artifacts is None
            or not isinstance(manifest.get("build"), dict)
            or not isinstance(manifest.get("sdk"), dict)
        ):
            raise RuntimeError(f"Differential {variant} build manifest is incomplete: {manifest_path}")
        validate_firmware_artifact_roles(
            artifacts,
            REQUIRED_DIFF_FIRMWARE_ROLES,
        )
        variants[variant] = {
            "build_root": str(variant_root.resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "manifest_sha256": _sha256(manifest_path),
            "manifest": manifest,
            "sdk": sdk_selection_provenance(selections[variant]),
        }

    command = [sys.executable, str(Path(__file__).resolve()), parsed.action, *parsed.targets, "--diff"]
    if parsed.config:
        command.extend(["--config", parsed.config])
    command.extend(["--build-root", str(build_root.resolve())])
    diff_manifest = {
        "schema_version": 1,
        "target": target,
        "build": {"root": str(build_root.resolve()), "command": command},
        "variants": variants,
    }
    output_path = build_root / DIFF_FW_DATA_FILENAME
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(diff_manifest, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(output_path)
    return output_path


def clean_build_root(build_root: Path) -> int:
    if not build_root.exists():
        return 0
    shutil.rmtree(build_root)
    return 1


def process_slcp(
    slcp: Path,
    *,
    build_root: Path,
    firmware_cfg: dict[str, str],
    sdk_path: Path,
    runtime_xapi_path: Path | None,
    action: str,
    sdk_selection: SDKSelection | None = None,
    vcom_metadata: dict | None = None,
) -> dict:
    out_dir = output_dir_for(slcp, build_root)
    started_at = time.perf_counter()
    generated_slcp = out_dir / slcp.name
    image = None
    metadata_path = None

    if action == "rebuild" and out_dir.exists():
        shutil.rmtree(out_dir)

    if action in {"generate", "upgrade", "rebuild"}:
        generate_slcp(slcp, out_dir=out_dir, board=firmware_cfg["board"], sdk_path=sdk_path)

    if action == "upgrade":
        generated_slcp = upgrade_slcp(slcp, out_dir=out_dir)

    if action == "build":
        image = build_project(slcp, out_dir=out_dir, board=firmware_cfg["board"], sdk_path=sdk_path)
    elif action == "rebuild":
        image = build_slcp(out_dir=out_dir)

    if image is not None:
        metadata_path = write_build_metadata(
            slcp=slcp,
            out_dir=out_dir,
            image=image,
            firmware_cfg=firmware_cfg,
            sdk_path=sdk_path,
            runtime_xapi_path=runtime_xapi_path,
            sdk_selection=sdk_selection,
            vcom_metadata=vcom_metadata,
        )

    return {
        "slcp": str(slcp),
        "out_dir": str(out_dir),
        "generated_slcp": str(generated_slcp),
        "image": str(image) if image else "",
        "metadata": str(metadata_path) if metadata_path else "",
        "sdk_path": str(sdk_path),
        "elapsed_s": round(time.perf_counter() - started_at, 2),
        "action": action,
    }


def process_sdk_app_profile(
    slcp: Path,
    *,
    build_root: Path,
    firmware_cfg: dict[str, str],
    sdk_path: Path,
    runtime_xapi_path: Path | None,
    action: str,
    sdk_selection: SDKSelection | None,
    vcom_metadata: dict | None,
    app_configuration: dict,
    app_config_path: Path,
    board_id: str,
    prs_configuration: str,
    profile_name: str,
    profile_config: dict,
) -> dict:
    project_name = APP_PROFILE_PROJECT_NAMES[profile_name]
    out_dir = build_root / project_name
    started_at = time.perf_counter()
    if action == "rebuild" and out_dir.exists():
        shutil.rmtree(out_dir)

    slc_options = merge_slc_options(
        app_configuration["board_config"],
        profile_config,
    )
    configuration = merge_app_configuration(
        app_configuration["app_config"],
        profile_config,
    )
    generate_slcp(
        slcp,
        out_dir=out_dir,
        board=board_id,
        sdk_path=sdk_path,
        project_name=project_name,
        slc_options=slc_options,
        configuration=configuration,
    )

    if action == "upgrade":
        upgrade_slcp(slcp, out_dir=out_dir)

    image = build_slcp(out_dir=out_dir) if action in {"build", "rebuild"} else None
    metadata_path = None
    if image is not None:
        metadata_path = write_build_metadata(
            slcp=slcp,
            out_dir=out_dir,
            image=image,
            firmware_cfg=firmware_cfg,
            sdk_path=sdk_path,
            runtime_xapi_path=runtime_xapi_path,
            sdk_selection=sdk_selection,
            vcom_metadata=vcom_metadata,
            project_name=project_name,
            app_configuration={
                "application": app_configuration["app_config"]["application"],
                "device_profile": profile_name,
                "board": board_id,
                "prs_configuration": prs_configuration,
                "config_file": _relative_to_repo(app_config_path),
                "slc_options": slc_options,
                "configuration": configuration,
                "configuration_conditions": app_configuration["app_config"].get(
                    "configuration_conditions", {}
                ),
            },
        )

    return {
        "slcp": str(slcp),
        "project_name": project_name,
        "firmware_role": profile_name,
        "out_dir": str(out_dir),
        "generated_slcp": str(out_dir / slcp.name),
        "image": str(image) if image else "",
        "metadata": str(metadata_path) if metadata_path else "",
        "sdk_path": str(sdk_path),
        "elapsed_s": round(time.perf_counter() - started_at, 2),
        "action": action,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate and build the configured SiSDK application and device profiles."
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["clean", "generate", "upgrade", "build", "rebuild"],
        default="build",
        help="Action to run for each selected app. Defaults to build.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="Optional app, profile, or bootloader target. Defaults to all configured profiles and the bootloader.",
    )
    parser.add_argument("--config", default="", help="cms_client_config.json path")
    parser.add_argument("--sdk-package-path", default="", help="explicit unpacked SiSDK package path")
    parser.add_argument(
        "--artifactory",
        action="store_true",
        help="obtain the SDK specified by artifactory.version and artifactory.build",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="build configured version1 and version2 SiSDK packages in isolated roots",
    )
    parser.add_argument("--sisdk-version", default="", help="override selected SiSDK version")
    parser.add_argument(
        "--build-number",
        "-bn",
        default="",
        help="override Artifactory SDK build number",
    )
    parser.add_argument("--build-root", default="", help="override firmware build root")
    parser.add_argument("--board", default="", help="override target board")
    parser.add_argument("--board-rev", default="", help="override target board revision")
    parser.add_argument("--device", default="", help="select one configured device profile")
    parser.add_argument("--target-device", default="", help="override target MCU part number metadata")
    for tool_name in TOOL_ENV_VARS:
        parser.add_argument(f"--{tool_name}", default="", help=f"override {tool_name} executable")
    return parser.parse_args(argv)


def run_firmware_action(
    parsed: argparse.Namespace,
    effective_config: dict,
    build_root: Path,
    *,
    sdk_selection: SDKSelection | None = None,
    required_firmware_roles: frozenset[str] = frozenset(),
) -> int:
    """Run one normal firmware action in the supplied, fully isolated build root."""

    total_start = time.perf_counter()
    if sdk_selection is None and parsed.artifactory:
        artifactory = effective_config.get("artifactory", {})
        sdk_selection = resolve_artifactory_sdk_selection(
            str(artifactory.get("version", "")),
            str(artifactory.get("build", "")),
        )
        effective_config["firmware"]["sisdk"] = sdk_selection.version

    firmware_cfg = validate_firmware_config(effective_config)
    if sdk_selection is None:
        sdk_selection = resolve_sdk_selection(
            version=firmware_cfg["sisdk"],
            cli_package_path=parsed.sdk_package_path,
            config_package_path=effective_config["firmware"].get("sisdk_package_path"),
        )
    if sdk_selection.version != firmware_cfg["sisdk"]:
        raise RuntimeError(
            "Selected SiSDK version does not match the firmware action configuration: "
            f"{sdk_selection.version} != {firmware_cfg['sisdk']}"
        )
    sdk_path = sdk_selection.path

    app_config, board_config, app_config_path = load_app_configuration(
        sdk_selection.version,
        firmware_cfg["board"],
    )
    application = app_config["application"].strip()
    profiles = board_config["profiles"]
    selected_profiles, include_bootloader = select_app_build_targets(
        parsed.targets,
        application=application,
        profiles=profiles,
        requested_device=parsed.device,
    )
    sdk_app_slcp = resolve_sdk_app_slcp(sdk_path, application)
    board_id = firmware_cfg["board"].strip().lower().split("_", 1)[0]
    app_config_context = {
        "app_config": app_config,
        "board_config": board_config,
    }

    if required_firmware_roles and parsed.device:
        raise RuntimeError("--diff requires all firmware profiles and cannot be combined with --device")

    trust_sisdk(sdk_path)
    runtime_xapi_path = None
    vcom_metadata = None
    if parsed.action in {"build", "rebuild"}:
        runtime_xapi_path = copy_bgapi_xapi(build_root, sdk_path)
        vcom_metadata = resolve_vcom_build_metadata(
            firmware_cfg=firmware_cfg,
            sdk_path=sdk_path,
            build_root=build_root,
        )

    summaries: list[dict] = []
    if include_bootloader:
        bootloader_slcp = resolve_bootloader_slcp()
        summary = process_slcp(
            bootloader_slcp,
            build_root=build_root,
            firmware_cfg=firmware_cfg,
            sdk_path=sdk_path,
            runtime_xapi_path=runtime_xapi_path,
            action=parsed.action,
            sdk_selection=sdk_selection,
            vcom_metadata=vcom_metadata,
        )
        summaries.append(summary)

    for profile_name in selected_profiles:
        profile_config = profiles[profile_name]
        if not isinstance(profile_config, dict):
            raise RuntimeError(f"SiSDK app profile {profile_name!r} must be an object")
        summary = process_sdk_app_profile(
            sdk_app_slcp,
            build_root=build_root,
            firmware_cfg=firmware_cfg,
            sdk_path=sdk_path,
            runtime_xapi_path=runtime_xapi_path,
            action=parsed.action,
            sdk_selection=sdk_selection,
            vcom_metadata=vcom_metadata,
            app_configuration=app_config_context,
            app_config_path=app_config_path,
            board_id=board_id,
            prs_configuration=board_config["prs_configuration"],
            profile_name=profile_name,
            profile_config=profile_config,
        )
        summaries.append(summary)

    for summary in summaries:
        print(
            f"{Path(summary['slcp']).name}: "
            f"action={summary['action']} "
            f"out={summary['out_dir']} "
            f"image={summary['image']} "
            f"metadata={summary['metadata']} "
            f"sdk={summary['sdk_path']} "
            f"elapsed_s={summary['elapsed_s']:.2f}"
        )

    if required_firmware_roles:
        validate_required_firmware_roles(summaries, required_firmware_roles)

    fw_data_path = None
    if parsed.action in {"build", "rebuild"} and runtime_xapi_path is not None:
        fw_data_path = write_fw_data(
            build_root=build_root,
            firmware_cfg=firmware_cfg,
            sdk_path=sdk_path,
            runtime_xapi_path=runtime_xapi_path,
            summaries=summaries,
            sdk_selection=sdk_selection,
            vcom_metadata=vcom_metadata,
        )

    print(
        "App manager: "
        f"action={parsed.action} "
        f"apps={len(summaries)} "
        f"board={firmware_cfg['board']} "
        f"sisdk={firmware_cfg['sisdk']} "
        f"fw_data={fw_data_path or ''} "
        f"build_root={build_root} "
        f"elapsed_s={time.perf_counter() - total_start:.2f}"
    )
    return 0


def build_diff_variants(
    parsed: argparse.Namespace,
    client_config: dict,
    build_root: Path,
) -> int:
    """Build each configured SDK variant below its own root without a shared manifest."""

    if parsed.sdk_package_path:
        raise RuntimeError(
            "cms BUILD --diff uses diff.sisdk_version1_path and diff.sisdk_version2_path; "
            "do not pass --sdk-package-path"
        )
    if parsed.action == "clean":
        raise RuntimeError("cms BUILD --diff does not support clean; remove one named variant root explicitly")
    if parsed.action not in {"build", "rebuild"}:
        raise RuntimeError("cms BUILD --diff supports only build or rebuild actions")
    selections = resolve_diff_sdk_selections(client_config)
    print("Differential SDK provenance:")
    print(
        json.dumps(
            {
                variant: sdk_selection_provenance(selection)
                for variant, selection in selections.items()
            },
            indent=2,
            sort_keys=True,
        )
    )
    for variant in ("version1", "version2"):
        selection = selections[variant]
        variant_values = vars(parsed).copy()
        variant_values.update(
            diff=False,
            sdk_package_path=str(selection.path),
            sisdk_version=selection.version,
            build_root=str(build_root / "diff" / variant),
        )
        variant_args = argparse.Namespace(**variant_values)
        variant_config = effective_build_config(client_config, variant_args)
        variant_root = Path(variant_config["firmware"]["build_root"])
        if not variant_root.is_absolute():
            variant_root = (REPO_ROOT / variant_root).resolve()
        print(f"Differential build variant={variant}")
        if run_firmware_action(
            variant_args,
            variant_config,
            variant_root,
            sdk_selection=selection,
            required_firmware_roles=REQUIRED_DIFF_FIRMWARE_ROLES,
        ) != 0:
            return 1
    diff_fw_data_path = write_diff_fw_data(
        build_root=build_root,
        parsed=parsed,
        client_config=client_config,
        selections=selections,
    )
    print(f"Differential build manifest={diff_fw_data_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parsed = parse_args(list(argv if argv is not None else sys.argv[1:]))
    if parsed.build_number and not parsed.artifactory:
        raise RuntimeError("--build-number can only be used with --artifactory")
    if parsed.artifactory and parsed.sdk_package_path:
        raise RuntimeError("--artifactory cannot be combined with --sdk-package-path")
    if parsed.artifactory and parsed.diff:
        raise RuntimeError("--artifactory cannot be combined with --diff")
    if parsed.artifactory and parsed.action == "clean":
        raise RuntimeError("clean does not use --artifactory")
    config_path = Path(parsed.config) if parsed.config else None
    client_config = read_client_config(config_path)
    if parsed.artifactory:
        artifactory = client_config.get("artifactory", {})
        artifactory = artifactory if isinstance(artifactory, dict) else {}
        version = str(parsed.sisdk_version or artifactory.get("version", "")).strip()
        build_number = str(parsed.build_number or artifactory.get("build", "")).strip()
        has_version = bool(version)
        has_build = bool(build_number)
        if not has_version or not has_build:
            raise RuntimeError(
                "cms_client_config.json must set artifactory.version and artifactory.build "
                "when --artifactory is used, or supply --sisdk-version and --build-number overrides"
            )
        if not re.fullmatch(r"\d{4}\.\d+", version):
            raise RuntimeError("artifactory.version must use the SiSDK release format YYYY.MM")
        if not build_number.isdecimal():
            raise RuntimeError("artifactory.build must be a numeric Artifactory build number")
    effective_config = effective_build_config(client_config, parsed)
    global _TOOL_OVERRIDES
    _TOOL_OVERRIDES = effective_config["tools"]
    build_root = Path(effective_config["firmware"]["build_root"])
    if not build_root.is_absolute():
        build_root = (REPO_ROOT / build_root).resolve()
    total_start = time.perf_counter()
    if parsed.diff:
        return build_diff_variants(parsed, client_config, build_root)
    if parsed.action == "clean":
        removed = clean_build_root(build_root)
        print(
            "App manager: "
            f"action=clean "
            f"build_root={build_root} "
            f"removed={removed} "
            f"elapsed_s={time.perf_counter() - total_start:.2f}"
        )
        return 0
    return run_firmware_action(parsed, effective_config, build_root)


if __name__ == "__main__":
    raise SystemExit(main())

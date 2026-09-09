import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.config_loader import BUILD_CONFIG_ENV, _load_json


APPS_DIR = REPO_ROOT / "apps"
DEFAULT_BUILD_ROOT = REPO_ROOT / ".fw_build"
DEFAULT_BOOTLOADER_SLCP = APPS_DIR / "bootloader-uart-bgapi.slcp"
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
    build = config.get("build", {}) if isinstance(config.get("build"), dict) else {}
    tools = dict(config.get("tools", {})) if isinstance(config.get("tools"), dict) else {}

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
        "device": str(override("device", target.get("device", ""))),
        "sisdk": str(override("sisdk_version", target.get("sisdk", target.get("sisdk_version", "")))),
        "sisdk_package_path": str(
            override("sdk_package_path", target.get("sisdk_package_path", target.get("sdk_package_path", "")))
        ),
        "build_root": str(override("build_root", build.get("root", config.get("build_root", DEFAULT_BUILD_ROOT)))),
        "bootloader_slcp": str(
            override("bootloader_slcp", build.get("bootloader_slcp", config.get("bootloader_slcp", DEFAULT_BOOTLOADER_SLCP)))
        ),
    }
    return {"target": dict(target), "build": dict(build), "tools": tools, "firmware": firmware}


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


def trust_sisdk(sdk_path: Path) -> None:
    run(["slc", "signature", "trust", "-s", str(sdk_path)], cwd=REPO_ROOT)


def resolve_bootloader_slcp(config: dict | None = None) -> Path:
    if config is None:
        effective_config = effective_build_config(read_client_config())
    elif "firmware" in config:
        effective_config = config
    else:
        effective_config = effective_build_config(config)
    configured = effective_config.get("firmware", {}).get("bootloader_slcp")
    path = Path(configured) if configured else DEFAULT_BOOTLOADER_SLCP
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
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


def generate_slcp(slcp: Path, *, out_dir: Path, board: str, sdk_path: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "slc",
        "generate",
        str(slcp),
        "--sdk-package-path",
        str(sdk_path),
        "-d",
        str(out_dir),
        f"-name={slcp.stem}",
        "--with",
        board,
        "--generator-timeout=60000",
    ]
    run(cmd, cwd=REPO_ROOT)


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
        "project_name": slcp.stem,
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
            "package_path": str(sdk_path.resolve()),
            "commit": sdk_selection.commit if sdk_selection else "",
            "hash": sdk_selection.sdk_hash if sdk_selection else "",
            "xapi_sha256": sdk_selection.xapi_hash if sdk_selection else _sha256(runtime_xapi_path),
        },
    }
    fw_data_path = build_root / FW_DATA_FILENAME
    fw_data_path.write_text(json.dumps(fw_data, indent=2) + "\n", encoding="utf-8")
    return fw_data_path


def firmware_artifacts_from_summaries(summaries: list[dict]) -> list[dict[str, str]]:
    """Describe known firmware roles from their declared SLCP project identity."""

    artifacts: list[dict[str, str]] = []
    for summary in summaries:
        image_path = str(summary.get("image", ""))
        project_name = Path(str(summary.get("slcp", ""))).stem.lower()
        role = FIRMWARE_ROLE_BY_PROJECT.get(project_name)
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage app generation, upgrade, and build for all .slcp files under apps.")
    parser.add_argument(
        "action",
        nargs="?",
        choices=["clean", "generate", "upgrade", "build", "rebuild"],
        default="build",
        help="Action to run for each app. Defaults to build.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="Optional .slcp files or directories. Defaults to all .slcp files under apps.",
    )
    parser.add_argument("--config", default="", help="cms_client_config.json path")
    parser.add_argument("--sdk-package-path", default="", help="explicit unpacked SiSDK package path")
    parser.add_argument(
        "--diff",
        action="store_true",
        help="build configured version1 and version2 SiSDK packages in isolated roots",
    )
    parser.add_argument("--sisdk-version", default="", help="override selected SiSDK version")
    parser.add_argument("--build-root", default="", help="override firmware build root")
    parser.add_argument("--bootloader-slcp", default="", help="override bootloader SLCP path")
    parser.add_argument("--board", default="", help="override target board")
    parser.add_argument("--board-rev", default="", help="override target board revision")
    parser.add_argument("--device", default="", help="override target device")
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
    trust_sisdk(sdk_path)
    runtime_xapi_path = None
    if parsed.action in {"build", "rebuild"}:
        runtime_xapi_path = copy_bgapi_xapi(build_root, sdk_path)

    slcps = discover_slcps(parsed.targets)
    if not slcps:
        print("No .slcp files found.")
        return 1

    summaries: list[dict] = []
    for slcp in slcps:
        summary = process_slcp(
            slcp,
            build_root=build_root,
            firmware_cfg=firmware_cfg,
            sdk_path=sdk_path,
            runtime_xapi_path=runtime_xapi_path,
            action=parsed.action,
            sdk_selection=sdk_selection,
        )
        summaries.append(summary)
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
        )

    print(
        "App manager: "
        f"action={parsed.action} "
        f"apps={len(slcps)} "
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
    config_path = Path(parsed.config) if parsed.config else None
    client_config = read_client_config(config_path)
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

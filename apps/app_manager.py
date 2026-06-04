import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.config_loader import load_effective_config


APPS_DIR = REPO_ROOT / "apps"
DEFAULT_BUILD_ROOT = REPO_ROOT / ".fw_build"
DEFAULT_BOOTLOADER_SLCP = APPS_DIR / "bootloader-uart-bgapi.slcp"
BUILD_METADATA_FILENAME = "build_metadata.json"
FW_DATA_FILENAME = "fw_data.json"
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
FIRMWARE_ARTIFACT_SUFFIXES = FIRMWARE_IMAGE_SUFFIXES + (".map", ".out")


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

    dev_cfg = load_effective_config(role=role)
    configured = dev_cfg.get("tools", {}).get(tool_name)
    resolved = _resolve_executable_candidate(configured)
    if resolved:
        return resolved

    resolved = shutil.which(tool_name)
    if resolved:
        return resolved

    raise RuntimeError(
        f"Required tool '{tool_name}' not found on PATH. "
        f"Configure dev_config.json tools.{tool_name}, set {env_name}, "
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


def read_dev_config(path: Path = REPO_ROOT / "dev_config.json") -> dict:
    if path != REPO_ROOT / "dev_config.json":
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return load_effective_config(role="build")


def resolve_build_root(cli_value: str | None) -> Path:
    dev_cfg = read_dev_config()
    build_root = dev_cfg.get("firmware", {}).get("build_root")
    if build_root:
        return Path(build_root)
    return DEFAULT_BUILD_ROOT


def validate_firmware_config(dev_cfg: dict) -> dict[str, str]:
    firmware = dev_cfg.get("firmware", {})
    if not isinstance(firmware, dict):
        firmware = {}

    resolved_board = firmware.get("board")
    device = firmware.get("device")
    sisdk = firmware.get("sisdk")
    brev = firmware.get("brev", "")

    missing: list[str] = []
    if not resolved_board:
        missing.append("firmware.board")
    if not device:
        missing.append("firmware.device")
    if not sisdk:
        missing.append("firmware.sisdk")

    if missing:
        missing_fields = ", ".join(missing)
        raise RuntimeError(
            "Missing required firmware settings in dev_config.json: "
            f"{missing_fields}. "
            "Set these values under the 'firmware' section."
        )

    return {
        "board": str(resolved_board),
        "brev": str(brev),
        "device": str(device),
        "sisdk": str(sisdk),
    }


def list_installed_sisdks() -> list[dict[str, str]]:
    result = run(["slt", "list", "simplicity-sdk"], cwd=REPO_ROOT)
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            if current and current.get("version") and current.get("path"):
                records.append(current)
            current = None
            continue
        if line.startswith("Id: "):
            if current and current.get("version") and current.get("path"):
                records.append(current)
            package_id = line.split(":", 1)[1].strip()
            current = {"id": package_id}
            continue
        if current is None or current.get("id") != "simplicity-sdk":
            continue
        if line.startswith("Version: "):
            current["version"] = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Installation:"):
            current["path"] = ""
            continue
        if "path" in current and not current["path"]:
            current["path"] = line

    if current and current.get("version") and current.get("path"):
        records.append(current)

    return records


def resolve_sisdk_path(version: str) -> Path:
    matches = [record for record in list_installed_sisdks() if record.get("version") == version]
    if not matches:
        raise RuntimeError(
            f"Requested Simplicity SDK version '{version}' is not installed. "
            "Install it with `slt install simplicity-sdk --version <version>` "
            "or update dev_config.json firmware.sisdk."
        )
    if len(matches) > 1:
        paths = ", ".join(record["path"] for record in matches)
        raise RuntimeError(
            f"Requested Simplicity SDK version '{version}' resolved to multiple installed paths: {paths}. "
            "Clean up duplicate installs before running app_manager.py."
        )
    sdk_path = Path(matches[0]["path"])
    if not sdk_path.exists():
        raise RuntimeError(
            f"Requested Simplicity SDK version '{version}' resolved to a missing path: {sdk_path}"
        )
    
    sl_bt_xapi : Path = sdk_path / SL_BT_XAPI_PATH
    if not sl_bt_xapi.exists():
        raise RuntimeError(
            f"Requested Simplicity SDK version '{version}' cannot find `sl_bt.xapi`: {sl_bt_xapi}"
        )

    return sdk_path


def trust_sisdk(sdk_path: Path) -> None:
    run(["slc", "signature", "trust", "-s", str(sdk_path)], cwd=REPO_ROOT)


def resolve_bootloader_slcp() -> Path:
    dev_cfg = read_dev_config()
    configured = dev_cfg.get("firmware", {}).get("bootloader_slcp")
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
        artifacts.extend(root.rglob(f"*{suffix}"))
    return sorted({path.resolve() for path in artifacts})


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
) -> Path:
    hash_by_role = {
        "dut_app_hash": "",
        "tester_app_hash": "",
        "bootloader_app_hash": "",
    }
    for summary in summaries:
        image_path = summary.get("image")
        if not image_path:
            continue
        image = Path(image_path)
        project_name = Path(summary["slcp"]).stem.lower()
        image_hash = _sha256(image)
        if "bootloader" in project_name:
            hash_by_role["bootloader_app_hash"] = image_hash
        elif "tester" in project_name:
            hash_by_role["tester_app_hash"] = image_hash
        elif "dut" in project_name:
            hash_by_role["dut_app_hash"] = image_hash

    fw_data = {
        "firmware": {
            "board": firmware_cfg["board"],
            "brev": firmware_cfg.get("brev", ""),
            "device": firmware_cfg["device"],
            "sisdk": firmware_cfg["sisdk"],
            **hash_by_role,
        },
    }
    fw_data_path = build_root / FW_DATA_FILENAME
    fw_data_path.write_text(json.dumps(fw_data, indent=2) + "\n", encoding="utf-8")
    return fw_data_path


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
            image=image
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
    return parser.parse_args(argv)

def main(argv: list[str] | None = None) -> int:
    parsed = parse_args(list(argv if argv is not None else sys.argv[1:]))
    build_root = resolve_build_root(None)
    if not build_root.is_absolute():
        build_root = (REPO_ROOT / build_root).resolve()
    total_start = time.perf_counter()
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

    dev_cfg = read_dev_config()
    firmware_cfg = validate_firmware_config(dev_cfg)
    sdk_path = resolve_sisdk_path(firmware_cfg["sisdk"])
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

    fw_data_path = None
    if parsed.action in {"build", "rebuild"} and runtime_xapi_path is not None:
        fw_data_path = write_fw_data(
            build_root=build_root,
            firmware_cfg=firmware_cfg,
            sdk_path=sdk_path,
            runtime_xapi_path=runtime_xapi_path,
            summaries=summaries,
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


if __name__ == "__main__":
    raise SystemExit(main())

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = REPO_ROOT / "apps"
DEFAULT_BUILD_ROOT = REPO_ROOT / ".fw_build"
DEFAULT_BOOTLOADER_SLCP = APPS_DIR / "bootloader-uart-bgapi.slcp"


def run(cmd: list[str], *, cwd: Path | None = None, ok_returncodes: set[int] | None = None) -> subprocess.CompletedProcess:
    exe = shutil.which(cmd[0])
    if exe is None:
        raise RuntimeError(
            f"Required tool '{cmd[0]}' not found on PATH. "
            "Ensure Simplicity Studio CLI tools and CMake are installed and available."
        )
    result = subprocess.run([exe, *cmd[1:]], cwd=cwd, check=False, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    accepted = {0}
    if ok_returncodes:
        accepted.update(ok_returncodes)
    if result.returncode not in accepted:
        raise subprocess.CalledProcessError(result.returncode, [exe, *cmd[1:]], output=result.stdout, stderr=result.stderr)
    return result


def read_dev_config(path: Path = REPO_ROOT / "dev_config.json") -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resolve_build_root(cli_value: str | None) -> Path:
    dev_cfg = read_dev_config()
    build_root = dev_cfg.get("firmware", {}).get("build_root")
    if build_root:
        return Path(build_root)
    return DEFAULT_BUILD_ROOT


def resolve_board(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    dev_cfg = read_dev_config()
    board = dev_cfg.get("firmware", {}).get("board")
    if board:
        return str(board)
    return "brd4182a"


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


def generate_slcp(slcp: Path, *, out_dir: Path, board: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    run(
        [
            "slc",
            "generate",
            str(slcp),
            "-d",
            str(out_dir),
            f"-name={slcp.stem}",
            "--with",
            board,
            "--generator-timeout=60000",
        ],
        cwd=REPO_ROOT,
    )


def upgrade_slcp(slcp: Path, *, out_dir: Path) -> Path:
    generated_slcp = out_dir / slcp.name
    shutil.copy2(slcp, generated_slcp)
    # SLC returns a non-zero code when the project is already current.
    run(["slc", "upgrade", "-p", str(generated_slcp.resolve())], cwd=REPO_ROOT, ok_returncodes={4294967282, -14})
    return generated_slcp


def build_slcp(*, out_dir: Path) -> Path:
    run(["cmake", "--workflow", "--preset", "project"], cwd=out_dir / "cmake_gcc")
    candidates = list((out_dir / "cmake_gcc").rglob("*.s37")) + list((out_dir / "cmake_gcc").rglob("*.hex"))
    if not candidates:
        raise RuntimeError(f"No firmware image found under {out_dir}")
    return candidates[0]


def build_project(slcp: Path, *, out_dir: Path, project_name: str, board: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    generate_slcp(slcp, out_dir=out_dir, board=board)
    upgrade_slcp(slcp, out_dir=out_dir)
    return build_slcp(out_dir=out_dir)


def clean_build_root(build_root: Path) -> int:
    if not build_root.exists():
        return 0
    shutil.rmtree(build_root)
    return 1


def process_slcp(slcp: Path, *, build_root: Path, board: str, action: str) -> dict:
    out_dir = output_dir_for(slcp, build_root)
    started_at = time.perf_counter()
    generated_slcp = out_dir / slcp.name
    image = None

    if action in {"generate", "upgrade", "build", "rebuild"}:
        if action == "rebuild" and out_dir.exists():
            shutil.rmtree(out_dir)
        generate_slcp(slcp, out_dir=out_dir, board=board)

    if action in {"upgrade", "build", "rebuild"}:
        generated_slcp = upgrade_slcp(slcp, out_dir=out_dir)

    if action in {"build", "rebuild"}:
        image = build_slcp(out_dir=out_dir)

    return {
        "slcp": str(slcp),
        "out_dir": str(out_dir),
        "generated_slcp": str(generated_slcp),
        "image": str(image) if image else "",
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
    parser.add_argument(
        "--board",
        default=None,
        help="Board to pass to slc generate, e.g. brd4182a.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    parsed = parse_args(list(argv if argv is not None else sys.argv[1:]))
    build_root = resolve_build_root(None)
    if not build_root.is_absolute():
        build_root = (REPO_ROOT / build_root).resolve()
    board = resolve_board(parsed.board)
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

    slcps = discover_slcps(parsed.targets)
    if not slcps:
        print("No .slcp files found.")
        return 1

    for slcp in slcps:
        summary = process_slcp(slcp, build_root=build_root, board=board, action=parsed.action)
        print(
            f"{Path(summary['slcp']).name}: "
            f"action={summary['action']} "
            f"out={summary['out_dir']} "
            f"image={summary['image']} "
            f"elapsed_s={summary['elapsed_s']:.2f}"
        )

    print(
        "App manager: "
        f"action={parsed.action} "
        f"apps={len(slcps)} "
        f"board={board} "
        f"build_root={build_root} "
        f"elapsed_s={time.perf_counter() - total_start:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

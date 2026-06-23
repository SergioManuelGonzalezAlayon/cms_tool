import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
CONFIG_ROOT = REPO_ROOT / "tests" / "configs"
RESULTS_DIR = REPO_ROOT / "artifacts" / "orchestrator"
DEFAULT_RESULTS_LOG = RESULTS_DIR / "results.jsonl"


@dataclass(frozen=True)
class Job:
    suite: str
    feature: str
    nodeid: str
    config_path: Path
    cfg_arg: str
    mode: str
    partition: str
    dut_slcp: str
    tester_slcp: str
    board: str
    supply_v: float | int | str
    dut_image: str = ""
    tester_image: str = ""
    dut_image_sha256: str = ""
    tester_image_sha256: str = ""
    cms_fw_metadata: str = ""

    @property
    def job_id(self) -> str:
        return f"{self.nodeid}|{self.cfg_arg}"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def discover_parameter_files(targets: list[Path]) -> list[Path]:
    parameter_files: list[Path] = []
    for target in targets:
        resolved = target if target.is_absolute() else (REPO_ROOT / target)
        if resolved.is_file():
            if resolved.name == "parameters.json":
                parameter_files.append(resolved.resolve())
            continue
        if resolved.exists():
            parameter_files.extend(path.resolve() for path in resolved.rglob("parameters.json"))
    return sorted(set(parameter_files))


def classify_mode(config_path: Path, config_dir: Path) -> str:
    relative = config_path.relative_to(config_dir)
    if relative.name == "default.json" and relative.parent == Path("."):
        return "default"
    if relative.parts and relative.parts[0] == "full_sweep":
        return "full_sweep"
    return "one_factor"


def partition_name(config_path: Path, config_dir: Path) -> str:
    relative = config_path.relative_to(config_dir)
    if len(relative.parts) >= 2 and relative.parts[0] == "full_sweep":
        return relative.parts[1]
    return ""


def cfg_argument(config_path: Path, config_dir: Path) -> str:
    return config_path.relative_to(config_dir).as_posix()


def infer_nodeid(config_dir: Path) -> tuple[str, str, str]:
    relative = config_dir.relative_to(CONFIG_ROOT)
    if len(relative.parts) != 2:
        raise ValueError(f"Unsupported config directory layout: {config_dir}")
    suite, feature = relative.parts
    return suite, feature, f"tests/{suite}.py::test_{feature}"


def discover_config_files(config_dir: Path, mode: str) -> list[Path]:
    config_files: list[Path] = []
    default_path = config_dir / "default.json"
    if mode in {"default", "all"} and default_path.exists():
        config_files.append(default_path)

    if mode in {"one_factor", "all"}:
        for path in sorted(config_dir.glob("*.json")):
            if path.name in {"default.json", "parameters.json"}:
                continue
            config_files.append(path)

    if mode in {"full_sweep", "all"}:
        sweep_dir = config_dir / "full_sweep"
        if sweep_dir.exists():
            config_files.extend(sorted(sweep_dir.rglob("*.json")))

    return config_files


def cms_firmware_metadata(path: str) -> dict:
    if not path:
        return {}
    metadata_path = Path(path)
    if not metadata_path.exists():
        raise FileNotFoundError(f"CMS firmware metadata does not exist: {metadata_path}")
    return load_json(metadata_path).get("firmware", {})


def build_jobs(
    targets: list[Path],
    *,
    mode: str,
    feature_filters: set[str],
    partition_filters: set[str],
    cms_metadata_path: str = "",
) -> list[Job]:
    cms_firmware = cms_firmware_metadata(cms_metadata_path)
    jobs: list[Job] = []
    for parameter_file in discover_parameter_files(targets):
        config_dir = parameter_file.parent
        suite, feature, nodeid = infer_nodeid(config_dir)
        if feature_filters and feature not in feature_filters:
            continue

        for config_path in discover_config_files(config_dir, mode):
            config = load_json(config_path)
            job_mode = classify_mode(config_path, config_dir)
            partition = partition_name(config_path, config_dir)
            if partition_filters and partition not in partition_filters:
                continue

            firmware = config.get("firmware", {})
            hw = config.get("hw", {})
            dut_slcp = str(cms_firmware.get("dut_slcp") or firmware.get("dut_slcp", ""))
            tester_slcp = str(cms_firmware.get("tester_slcp") or firmware.get("tester_slcp", ""))
            jobs.append(
                Job(
                    suite=suite,
                    feature=feature,
                    nodeid=nodeid,
                    config_path=config_path,
                    cfg_arg=cfg_argument(config_path, config_dir),
                    mode=job_mode,
                    partition=partition,
                    dut_slcp=dut_slcp,
                    tester_slcp=tester_slcp,
                    board=str(cms_firmware.get("board") or firmware.get("board", "")),
                    supply_v=hw.get("supply_v", ""),
                    dut_image=str(cms_firmware.get("dut_image", "")),
                    tester_image=str(cms_firmware.get("tester_image", "")),
                    dut_image_sha256=str(cms_firmware.get("dut_image_sha256", "")),
                    tester_image_sha256=str(cms_firmware.get("tester_image_sha256", "")),
                    cms_fw_metadata=cms_metadata_path,
                )
            )
    return sorted(jobs, key=job_sort_key)


def voltage_sort_value(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def job_sort_key(job: Job):
    return (
        job.suite,
        job.feature,
        job.dut_slcp,
        job.tester_slcp,
        job.board,
        job.partition,
        voltage_sort_value(job.supply_v),
        job.cfg_arg,
    )


def completed_job_ids(results_log: Path) -> set[str]:
    if not results_log.exists():
        return set()
    completed: set[str] = set()
    for line in results_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("status") == "passed":
            completed.add(record["job_id"])
    return completed


def append_result(results_log: Path, record: dict) -> None:
    results_log.parent.mkdir(parents=True, exist_ok=True)
    with results_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def print_plan(jobs: list[Job]) -> None:
    print(f"Planned jobs: {len(jobs)}")
    last_group = None
    for index, job in enumerate(jobs, start=1):
        group = (job.feature, job.dut_slcp, job.partition, job.supply_v)
        if group != last_group:
            print(
                f"\n[{job.feature}] dut={job.dut_slcp or '-'} "
                f"tester={job.tester_slcp or '-'} board={job.board or '-'} "
                f"partition={job.partition or '-'} v={job.supply_v}"
            )
            last_group = group
        print(f"  {index:04d} {job.cfg_arg}")
        if job.cms_fw_metadata:
            print(
                f"       cms dut_hash={job.dut_image_sha256[:12] or '-'} "
                f"tester_hash={job.tester_image_sha256[:12] or '-'}"
            )


def run_config_manager(mode: str, targets: list[Path]) -> None:
    if mode == "default":
        print("Skipping config generation for mode=default; using existing default.json files.")
        return

    config_mode = "both" if mode == "all" else mode
    cmd = [
        sys.executable,
        str(REPO_ROOT / "tests" / "configs" / "config_manager.py"),
        "generate",
        "--mode",
        config_mode,
    ]
    cmd.extend(str(target) for target in targets)
    print("Generating configs:", " ".join(cmd))
    subprocess.run(cmd, cwd=REPO_ROOT, check=True, text=True)


def run_job(job: Job, pytest_args: list[str]) -> tuple[int, float]:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        job.nodeid,
        "--cfg",
        job.cfg_arg,
        "-o",
        "log_cli=true",
        "--log-cli-level=INFO",
    ]
    cmd.extend(pytest_args)
    started = time.perf_counter()
    result = subprocess.run(cmd, cwd=REPO_ROOT, check=False, text=True)
    return result.returncode, time.perf_counter() - started


def strip_forwarded_cfg_args(pytest_args: list[str]) -> tuple[list[str], list[str]]:
    """Drop forwarded pytest --cfg values so orchestrated jobs keep their own config."""
    stripped: list[str] = []
    removed: list[str] = []
    index = 0
    while index < len(pytest_args):
        arg = pytest_args[index]
        if arg == "--cfg":
            if index + 1 < len(pytest_args):
                removed.append(pytest_args[index + 1])
                index += 2
            else:
                removed.append("")
                index += 1
            continue
        if arg.startswith("--cfg="):
            removed.append(arg.split("=", 1)[1])
            index += 1
            continue
        stripped.append(arg)
        index += 1
    return stripped, removed


def parse_args_with_pytest(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description="Generate, order, and execute config-driven pytest campaigns.")
    parser.add_argument(
        "targets",
        nargs="*",
        default=["tests/configs"],
        help="Config roots, config directories, or parameters.json files to orchestrate.",
    )
    parser.add_argument(
        "--mode",
        choices=["default", "one_factor", "full_sweep", "all"],
        default="all",
        help="Which generated config set to run. Defaults to all.",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generate configs through tests/configs/config_manager.py before discovery.",
    )
    parser.add_argument(
        "--feature",
        action="append",
        default=[],
        help="Only run a specific feature, for example beacon. Repeat to include more.",
    )
    parser.add_argument(
        "--partition",
        action="append",
        default=[],
        help="Only run a specific full_sweep partition such as dcdc or no_dcdc.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the execution order without running pytest.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip jobs already recorded as passed in the results log.",
    )
    parser.add_argument(
        "--max-fail",
        type=int,
        default=0,
        help="Stop after this many failures. Zero means no early stop.",
    )
    parser.add_argument(
        "--results-log",
        default=str(DEFAULT_RESULTS_LOG),
        help="Path to the JSONL results log.",
    )
    parser.add_argument(
        "--cms-runtime-config",
        default="",
        help="Bench runtime config for a CMS remote pytest run.",
    )
    parser.add_argument(
        "--cms-fw-metadata",
        default="",
        help="Firmware metadata generated by the CMS daemon for a remote pytest run.",
    )
    parser.add_argument(
        "--upload-results-to-database",
        action="store_true",
        default=False,
        help="Upload generated results JSON files to the configured database/data lake.",
    )

    parsed, pytest_args = parser.parse_known_args(argv)
    return parsed, pytest_args


def cms_pytest_args(parsed: argparse.Namespace) -> list[str]:
    args: list[str] = []
    if parsed.cms_runtime_config:
        args.extend(["--cms-runtime-config", parsed.cms_runtime_config])
    if parsed.cms_fw_metadata:
        args.extend(["--cms-fw-metadata", parsed.cms_fw_metadata])
    if parsed.upload_results_to_database:
        args.append("--upload-results-to-database")
    return args


def main(argv: list[str] | None = None) -> int:
    parsed, pytest_args = parse_args_with_pytest(list(argv if argv is not None else sys.argv[1:]))
    targets = [Path(target) for target in parsed.targets]
    results_log = Path(parsed.results_log)
    pytest_args, ignored_cfg_args = strip_forwarded_cfg_args(pytest_args)
    if ignored_cfg_args:
        print(
            "Ignoring forwarded --cfg argument(s); "
            "test_orchestrator selects configs from discovered jobs."
        )
    pytest_args = [*cms_pytest_args(parsed), *pytest_args]

    if parsed.generate:
        run_config_manager(parsed.mode, targets)

    jobs = build_jobs(
        targets,
        mode=parsed.mode,
        feature_filters=set(parsed.feature),
        partition_filters=set(parsed.partition),
        cms_metadata_path=parsed.cms_fw_metadata,
    )
    if not jobs:
        print("No orchestrated jobs found.")
        return 1

    if parsed.resume:
        done = completed_job_ids(results_log)
        jobs = [job for job in jobs if job.job_id not in done]
        if not jobs:
            print("All matching jobs are already marked as passed in the results log.")
            return 0

    print_plan(jobs)
    if parsed.dry_run:
        return 0

    failures = 0
    for index, job in enumerate(jobs, start=1):
        print(f"\nRunning {index}/{len(jobs)}: {job.nodeid} --cfg {job.cfg_arg}")
        returncode, duration_s = run_job(job, pytest_args)
        status = "passed" if returncode == 0 else "failed"
        append_result(
            results_log,
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "job_id": job.job_id,
                "nodeid": job.nodeid,
                "cfg_path": str(job.config_path),
                "cfg_arg": job.cfg_arg,
                "mode": job.mode,
                "partition": job.partition,
                "dut_slcp": job.dut_slcp,
                "tester_slcp": job.tester_slcp,
                "board": job.board,
                "supply_v": job.supply_v,
                "dut_image": job.dut_image,
                "tester_image": job.tester_image,
                "dut_image_sha256": job.dut_image_sha256,
                "tester_image_sha256": job.tester_image_sha256,
                "cms_fw_metadata": job.cms_fw_metadata,
                "exit_code": returncode,
                "duration_s": round(duration_s, 3),
                "status": status,
            },
        )
        if returncode != 0:
            failures += 1
            if parsed.max_fail and failures >= parsed.max_fail:
                print(f"Stopping early after {failures} failure(s).")
                return 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

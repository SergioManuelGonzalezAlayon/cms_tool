import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from cms_protocol import ResultType, TestCaseIdentity, TestCaseState, test_case_identity_to_dict


# This is an executable application module, not a pytest test module.
__test__ = False


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
    config_hash: str = ""
    requested_config_hash: str = ""
    measurement_id: str = ""
    run_name: str = ""
    measurement_created_at: str = ""

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


def exact_config_path(config_dir: Path, exact_config: str) -> Path:
    """Resolve a user-selected config name beneath one feature config directory."""
    requested = exact_config.strip().replace("\\", "/")
    if not requested:
        raise ValueError("exact config must not be empty")
    if not requested.endswith(".json"):
        requested = f"{requested}.json"

    relative = Path(requested)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("exact config must be a single relative config path")

    resolved = (config_dir / relative).resolve()
    try:
        resolved.relative_to(config_dir.resolve())
    except ValueError as exc:
        raise ValueError("exact config must stay beneath the selected config directory") from exc
    return resolved


def cms_execution_metadata(path: str) -> dict:
    if not path:
        return {}
    metadata_path = Path(path)
    if not metadata_path.exists():
        raise FileNotFoundError(f"CMS firmware metadata does not exist: {metadata_path}")
    return load_json(metadata_path)


def cms_firmware_metadata(path: str) -> dict:
    return cms_execution_metadata(path).get("firmware", {})


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_jobs(
    targets: list[Path],
    *,
    mode: str,
    feature_filters: set[str],
    partition_filters: set[str],
    exact_config: str = "",
    cms_metadata_path: str = "",
) -> list[Job]:
    cms_metadata = cms_execution_metadata(cms_metadata_path)
    cms_firmware = cms_metadata.get("firmware", {})
    requested_selector = cms_metadata.get("request", {}).get("selector", {})
    requested_config_hash = str(requested_selector.get("config_hash", ""))
    measurement_identity = cms_metadata.get("identity", {})
    jobs: list[Job] = []
    for parameter_file in discover_parameter_files(targets):
        config_dir = parameter_file.parent
        suite, feature, nodeid = infer_nodeid(config_dir)
        if feature_filters and feature not in feature_filters:
            continue

        config_paths = discover_config_files(config_dir, mode)
        if exact_config:
            selected_config_path = exact_config_path(config_dir, exact_config)
            config_paths = [
                config_path
                for config_path in config_paths
                if config_path.resolve() == selected_config_path
            ]
            if not config_paths:
                raise ValueError(f"exact config was not found: {exact_config}")

        for config_path in config_paths:
            config = load_json(config_path)
            job_mode = classify_mode(config_path, config_dir)
            partition = partition_name(config_path, config_dir)
            if partition_filters and partition not in partition_filters:
                continue

            firmware = config.get("firmware", {})
            hw = config.get("hw", {})
            dut_slcp = str(cms_firmware.get("dut_slcp") or firmware.get("dut_slcp", ""))
            tester_slcp = str(cms_firmware.get("tester_slcp") or firmware.get("tester_slcp", ""))
            config_hash = sha256_file(config_path)
            if requested_config_hash and config_hash.lower() != requested_config_hash.lower():
                raise ValueError(
                    "requested config hash does not match selected config: "
                    f"{cfg_argument(config_path, config_dir)}"
                )
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
                    config_hash=config_hash,
                    requested_config_hash=requested_config_hash,
                    measurement_id=str(measurement_identity.get("measurement_id", "")),
                    run_name=str(measurement_identity.get("run_name", "")),
                    measurement_created_at=str(measurement_identity.get("created_at", "")),
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


def completed_job_records(results_log: Path) -> list[dict]:
    if not results_log.exists():
        return []
    completed: list[dict] = []
    for line in results_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        record_type = record.get("record_type")
        if record_type == "job_completed" or (
            record_type is None and record.get("status") in {"passed", "failed"}
        ):
            completed.append(record)
    return completed


def completed_job_ids(results_log: Path) -> set[str]:
    """Return passed job IDs for resume, including legacy completion records."""
    return {
        str(record["job_id"])
        for record in completed_job_records(results_log)
        if record.get("status") == TestCaseState.PASSED.value and record.get("job_id")
    }


def append_result(results_log: Path, record: dict) -> None:
    results_log.parent.mkdir(parents=True, exist_ok=True)
    with results_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def plan_path_for_results(results_log: Path) -> Path:
    return results_log.with_name("orchestrator_plan.json")


def sanitized_config_identity(config: str) -> str:
    config_without_suffix = Path(config).with_suffix("").as_posix()
    sanitized = re.sub(r"[^A-Za-z0-9]+", "-", config_without_suffix).strip("-").lower()
    return sanitized[:100].rstrip("-") or "config"


def junit_path_for_job(results_log: Path, job: Job, ordinal: int) -> Path:
    filename = f"{ordinal:04d}-{sanitized_config_identity(job.cfg_arg)}.xml"
    return results_log.parent / "junit" / filename


def job_record(
    job: Job,
    ordinal: int,
    total_jobs: int,
    junit_path: Path | None = None,
) -> dict:
    record = asdict(job)
    record["config_path"] = str(job.config_path)
    record["cfg_path"] = str(job.config_path)
    if junit_path is not None:
        record["junit_path"] = str(junit_path)
    record.update(
        test_case_identity_to_dict(
            TestCaseIdentity(
                job_id=job.job_id,
                ordinal=ordinal,
                total_jobs=total_jobs,
                feature=job.feature,
                config=job.cfg_arg,
                config_hash=job.config_hash,
                mode=job.mode,
                partition=job.partition,
            )
        )
    )
    return record


def build_job_plan(jobs: list[Job], results_log: Path | None = None) -> dict:
    total_jobs = len(jobs)
    return {
        "record_type": "job_plan",
        "schema_version": 1,
        "total_jobs": total_jobs,
        "jobs": [
            {
                **job_record(
                    job,
                    ordinal,
                    total_jobs,
                    junit_path_for_job(results_log, job, ordinal) if results_log is not None else None,
                ),
                "status": TestCaseState.PENDING.value,
            }
            for ordinal, job in enumerate(jobs, start=1)
        ],
    }


def write_job_plan(results_log: Path, jobs: list[Job]) -> Path:
    plan_path = plan_path_for_results(results_log)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(build_job_plan(jobs, results_log), indent=2) + "\n", encoding="utf-8")
    return plan_path


def job_started_record(
    job: Job,
    ordinal: int,
    total_jobs: int,
    started_at: str,
    junit_path: Path,
) -> dict:
    record = job_record(job, ordinal, total_jobs, junit_path)
    record.update(
        {
            "record_type": "job_started",
            "timestamp": started_at,
            "started_at": started_at,
            "status": TestCaseState.RUNNING.value,
        }
    )
    return record


def job_completed_record(
    job: Job,
    ordinal: int,
    total_jobs: int,
    returncode: int,
    duration_s: float,
    started_at: str,
    finished_at: str,
    junit_path: Path,
) -> dict:
    status = TestCaseState.PASSED if returncode == 0 else TestCaseState.FAILED
    record = job_record(job, ordinal, total_jobs, junit_path)
    record.update(
        {
            "record_type": "job_completed",
            "timestamp": finished_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": returncode,
            "duration_s": round(duration_s, 3),
            "status": status.value,
            "result_type": ResultType.PYTEST_JUNIT.value,
        }
    )
    return record


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


def run_job(job: Job, pytest_args: list[str], junit_path: Path) -> tuple[int, float]:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-o",
        "addopts=",
        job.nodeid,
        "--cfg",
        job.cfg_arg,
        "-o",
        "log_cli=true",
        "--log-cli-level=INFO",
    ]
    cmd.extend(pytest_args)
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    cmd.extend(["--junitxml", str(junit_path)])
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
        "--exact-config",
        default="",
        help="Run exactly one config path or name from the selected feature directory.",
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
        if parsed.cms_fw_metadata:
            print(
                "CMS orchestrator runs must use --exact-config for selected "
                "configs; forwarded pytest --cfg argument(s) are not allowed."
            )
            return 2
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
        exact_config=parsed.exact_config,
        cms_metadata_path=parsed.cms_fw_metadata,
    )
    if not jobs:
        print("No orchestrated jobs found.")
        return 1

    plan_path = write_job_plan(results_log, jobs)
    print(f"Wrote complete job plan: {plan_path}")
    planned_positions = {job.job_id: (ordinal, len(jobs)) for ordinal, job in enumerate(jobs, start=1)}
    junit_paths = {
        job.job_id: junit_path_for_job(results_log, job, ordinal)
        for ordinal, job in enumerate(jobs, start=1)
    }

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
    for job in jobs:
        ordinal, total_jobs = planned_positions[job.job_id]
        junit_path = junit_paths[job.job_id]
        print(f"\nRunning {ordinal}/{total_jobs}: {job.nodeid} --cfg {job.cfg_arg}")
        started_at = datetime.now(timezone.utc).isoformat()
        append_result(
            results_log,
            job_started_record(job, ordinal, total_jobs, started_at, junit_path),
        )
        returncode, duration_s = run_job(job, pytest_args, junit_path)
        finished_at = datetime.now(timezone.utc).isoformat()
        append_result(
            results_log,
            job_completed_record(
                job,
                ordinal,
                total_jobs,
                returncode,
                duration_s,
                started_at,
                finished_at,
                junit_path,
            ),
        )
        if returncode != 0:
            failures += 1
            if parsed.max_fail and failures >= parsed.max_fail:
                print(f"Stopping early after {failures} failure(s).")
                return 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

from cms_protocol import ResultType


log = logging.getLogger(__name__)

CMS_BENCH_ID_ENV_VAR = "CURRENT_MEASUREMENT_CMS_BENCH_ID"

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

@dataclass
class ResultContext:
    test_name: str | None = None
    config_name: str | None = None
    config_path: Path | None = None
    config_data: dict = field(default_factory=dict)
    supply_v: float | None = None
    campaign_id: str | None = None


def _relative_path(path: Path | str | None) -> str | None:
    if path is None:
        return None
    return Path(path).as_posix()


def _config_protocol_and_test_case(config_path: Path) -> tuple[str, str]:
    parts = config_path.parts
    if "configs" in parts:
        index = parts.index("configs")
        protocol = parts[index + 1] if len(parts) > index + 1 else ""
        test_case = parts[index + 2] if len(parts) > index + 2 else ""
        if protocol or test_case:
            return protocol, test_case
    return "", config_path.parent.name


def _result_storage_metadata(
    *,
    test_name: str,
    config_name: str,
    config_path: Path,
    firmware: dict,
    bench: dict | None = None,
    slcp_meta: dict,
) -> dict:
    protocol, test_case = _config_protocol_and_test_case(config_path)
    _, partition = _config_mode_and_partition(config_path)
    bench = bench or {}
    cms_bench_id = (
        bench.get("bench_id")
        or firmware.get("cms_bench_id")
        or firmware.get("bench_id")
        or os.environ.get(CMS_BENCH_ID_ENV_VAR)
    )
    board_revision = (
        firmware.get("board_revision")
        or firmware.get("board_rev")
        or firmware.get("brev")
    )
    part_number = (
        firmware.get("part_number")
        or firmware.get("device")
        or slcp_meta.get("chip_id")
    )
    return {
        "prefix": "cms",
        "cms_bench_id": cms_bench_id,
        "protocol": protocol,
        "test_case": test_case or test_name,
        "partition": partition,
        "part_number": part_number,
        "board": firmware.get("board"),
        "board_revision": board_revision,
        "serial_number": firmware.get("serial_number"),
        "test_name": test_name,
        "config_name": config_name,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_slcp_metadata(path_str: str | None) -> dict:
    if not path_str:
        return {"sdk_name": None, "sdk_version": None, "chip_id": None}

    path = Path(path_str)
    if not path.exists():
        return {"sdk_name": None, "sdk_version": None, "chip_id": None}

    sdk_name = None
    sdk_version = None
    chip_id = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if sdk_name is None and stripped.startswith("id:"):
            sdk_name = stripped.split(":", 1)[1].strip() or None
        elif sdk_version is None and stripped.startswith("version:"):
            sdk_version = stripped.split(":", 1)[1].strip() or None
        elif chip_id is None and stripped.startswith("- id: EFR32"):
            chip_id = stripped.split(":", 1)[1].strip() or None
    return {
        "sdk_name": sdk_name,
        "sdk_version": sdk_version,
        "chip_id": chip_id,
    }


def _config_mode_and_partition(config_path: Path) -> tuple[str, str | None]:
    parts = config_path.parts
    if "full_sweep" in parts:
        index = parts.index("full_sweep")
        partition = parts[index + 1] if len(parts) > index + 1 else None
        return "full_sweep", partition
    if config_path.name == "default.json":
        return "default", None
    return "one_factor", None


def _current_mean_ma_from_label(analyzer, token: str):
    for label, stats in analyzer.aggr_sub_event_stats.items():
        if token in label.upper():
            return stats["current"]["mean"]
    return None


def _json_ready(value):
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)):
        return _json_ready(value.tolist())
    if hasattr(value, "item") and callable(value.item):
        try:
            return value.item()
        except Exception:
            pass
    return value

def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}

def _upload_results_to_database_from_env() -> bool:
    return (
        _env_flag("CURRENT_MEASUREMENT_UPLOAD_RESULTS_TO_DATABASE")
        or _env_flag("CURRRENT_MEASUREMENT_UPLOAD_RESULTS_TO_DATABASE")
    )


def _upload_status_path(out_dir: Path, artifact_stem: str) -> Path:
    return out_dir / f"{artifact_stem}_upload_status.json"


def _write_upload_status(
    path: Path,
    *,
    status: str,
    phase: str,
    result_json_path: Path | None = None,
    png_path: Path | None = None,
    error: str = "",
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": status,
                "phase": phase,
                "result_json_path": _relative_path(result_json_path),
                "png_path": _relative_path(png_path),
                "upload_results_to_database": status != "disabled",
                "error": error,
                "updated_at": utc_now(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

def _result_phy(test_name: str, advertiser: dict, scanner: dict, connection: dict):
    if test_name == "scanner":
        return scanner.get("phy")
    if test_name in {"conn_central", "conn_peripheral"}:
        return connection.get("phy")
    return advertiser.get("phy") or connection.get("phy") or scanner.get("phy")


def _upload_application_variant(firmware: dict) -> str:
    application_variants = {
        "ncp_ble_dut_dcdc.slcp": "dcdc",
        "ncp_ble_dut.slcp": "nodcdc",
    }
    dut_slcp_name = Path(str(firmware["dut_slcp"])).name.lower()
    try:
        return application_variants[dut_slcp_name]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported DUT SLCP for upload application variant: {dut_slcp_name}"
        ) from exc


def _upload_metadata(
    *,
    config_path: Path,
    config_data: dict,
    measured_at: str,
    ) -> dict[str, object]:
        firmware = config_data["firmware"]
        bench = config_data["bench"]
        config_parts = config_path.parts
        configs_index = config_parts.index("configs")
        protocol = config_parts[configs_index + 1]
        test_case = config_parts[configs_index + 2]
        return {
            "cms_bench_id": bench["bench_id"],
            "sisdk_version": firmware["sisdk_version"],
            "part_number": firmware["part_number"],
            "board": firmware["board"],
            "board_revision": firmware["board_revision"],
            "serial_number": firmware["serial_number"],
            "protocol": protocol,
            "application_variant": _upload_application_variant(firmware),
            "test_case": test_case,
            "measured_at": measured_at,
    }


def upload_result_json_to_database(
    result_json_path: Path | str,
    *,
    cms_bench_id: object,
    sisdk_version: object,
    part_number: object,
    board: object,
    board_revision: object,
    serial_number: object,
    protocol: object,
    application_variant: object,
    test_case: object,
    measured_at: str,
):
    return upload_result_file_to_database(
        result_json_path,
        cms_bench_id=cms_bench_id,
        sisdk_version=sisdk_version,
        part_number=part_number,
        board=board,
        board_revision=board_revision,
        serial_number=serial_number,
        protocol=protocol,
        application_variant=application_variant,
        test_case=test_case,
        measured_at=measured_at,
    )


def upload_result_file_to_database(
    result_file_path: Path | str,
    *,
    cms_bench_id: object,
    sisdk_version: object,
    part_number: object,
    board: object,
    board_revision: object,
    serial_number: object,
    protocol: object,
    application_variant: object,
    test_case: object,
    measured_at: str,
):
    from s3_upload import upload_file_to_data_lake

    return upload_file_to_data_lake(
        result_file_path,
        cms_bench_id=cms_bench_id,
        sisdk_version=sisdk_version,
        part_number=part_number,
        board=board,
        board_revision=board_revision,
        serial_number=serial_number,
        protocol=protocol,
        application_variant=application_variant,
        test_case=test_case,
        measured_at=measured_at,
    )

def _representative_indices(total: int, count: int) -> list[int]:
    if total <= 0 or count <= 0:
        return []
    if count >= total:
        return list(range(total))
    if count == 1:
        return [total // 2]
    positions = []
    for idx in range(count):
        pos = round(idx * (total - 1) / (count - 1))
        positions.append(pos)
    return sorted(set(positions))


def _capture_dataset_candidates(config_path: Path) -> tuple[list[Path], str]:
    mode, partition = _config_mode_and_partition(config_path)
    if mode == "full_sweep":
        candidates = sorted(config_path.parent.glob("*.json"), key=lambda path: path.name.lower())
        scope = f"full_sweep_partition:{partition}" if partition else "full_sweep_partition"
    elif mode == "one_factor":
        candidates = sorted(
            (
                path for path in config_path.parent.glob("*.json")
                if path.name not in {"default.json", "parameters.json"}
            ),
            key=lambda path: path.name.lower(),
        )
        scope = "one_factor"
    else:
        candidates = [config_path]
        scope = "default"
    if not candidates:
        candidates = [config_path]
    if config_path not in candidates:
        candidates.append(config_path)
        candidates = sorted(candidates, key=lambda path: path.name.lower())
    return candidates, scope


def _capture_policy(config_path: Path, *, count: int = 1) -> dict:
    candidates, scope = _capture_dataset_candidates(config_path)
    selected_indices = _representative_indices(len(candidates), count)
    selected_paths = [candidates[idx] for idx in selected_indices]
    current_index = candidates.index(config_path)
    retained = current_index in selected_indices
    return {
        "policy_name": "linear_representative_configs",
        "scope": scope,
        "requested_capture_count": count,
        "dataset_size": len(candidates),
        "dataset_index": current_index,
        "selected_indices": selected_indices,
        "selected_configs": [_relative_path(path) for path in selected_paths],
        "retained": retained,
    }


def _save_capture_npz(profile: dict, out_dir: Path, artifact_stem: str) -> Path:
    capture_path = out_dir / f"{artifact_stem}_capture.npz"
    if not profile:
        raise RuntimeError("Cannot save capture NPZ without any captured signals")
    arrays = {}
    for signal_name, signal_data in profile.items():
        signal_key = signal_name.replace("[", "_").replace("]", "").replace("/", "_")
        arrays[f"{signal_key}__samples"] = signal_data["samples"]
        arrays[f"{signal_key}__timestamp"] = signal_data["timestamp"]
    np.savez_compressed(capture_path, **arrays)
    return capture_path


def _build_result_summary(analyzer) -> list[dict]:
    rows = [
        {
            "meas_type": "Event",
            "count": analyzer.aggr_event_stats["current"]["N"],
            "i_mean_ma": analyzer.aggr_event_stats["current"]["mean"],
            "i_std_ma": analyzer.aggr_event_stats["current"]["std"],
            "t_mean_us": analyzer.aggr_event_stats["duration"]["mean"],
            "t_std_us": analyzer.aggr_event_stats["duration"]["std"],
            "e_mean_nj": analyzer.aggr_event_stats["energy"]["mean"],
            "e_std_nj": analyzer.aggr_event_stats["energy"]["std"],
        },
        {
            "meas_type": "Interval",
            "count": analyzer.aggr_interval_stats["current"]["N"],
            "i_mean_ma": analyzer.aggr_interval_stats["current"]["mean"],
            "i_std_ma": analyzer.aggr_interval_stats["current"]["std"],
            "t_mean_us": analyzer.aggr_interval_stats["duration"]["mean"],
            "t_std_us": analyzer.aggr_interval_stats["duration"]["std"],
            "e_mean_nj": analyzer.aggr_interval_stats["energy"]["mean"],
            "e_std_nj": analyzer.aggr_interval_stats["energy"]["std"],
        },
    ]
    for label, stats in analyzer.aggr_sub_event_stats.items():
        rows.append(
            {
                "meas_type": f"Subevent_{label}",
                "count": stats["current"]["N"],
                "i_mean_ma": stats["current"]["mean"],
                "i_std_ma": stats["current"]["std"],
                "t_mean_us": stats["duration"]["mean"],
                "t_std_us": stats["duration"]["std"],
                "e_mean_nj": stats["energy"]["mean"],
                "e_std_nj": stats["energy"]["std"],
            }
        )
    rows.append(
        {
            "meas_type": "Global",
            "count": None,
            "i_mean_ma": analyzer.get_global_avg_current() * 1e3,
            "i_std_ma": None,
            "t_mean_us": None,
            "t_std_us": None,
            "e_mean_nj": None,
            "e_std_nj": None,
        }
    )
    return rows


def _build_window_rows(analyzer) -> list[dict]:
    rows = []
    outlier_report = analyzer.outlier_report
    outlier_indices = set()
    if outlier_report and outlier_report.get("outlier_windows"):
            outlier_indices = {w["index"] for w in outlier_report["outlier_windows"]}

    for index, stats in enumerate(analyzer.window_stats, start=1):
        sub_windows = analyzer.sub_window_stats[index - 1] if len(analyzer.sub_window_stats) >= index else []
        rows.append(
            {
                "window_index": index,
                "profile_type": "event",
                "timestamp_s": stats["start"],
                "i_window_mean_ma": stats["avg_current"] * 1e3,
                "t_duration_us": stats["duration"] * 1e6,
                "e_nj": stats["energy"] * 1e9,
                "is_outlier": (index - 1) in outlier_indices,
                "subwindow_stats_json": sub_windows,
            }
        )
    return rows


def _select_plot_window(
    window_rows: list[dict],
    plot_metadata: dict | None = None,
) -> dict | None:
    """Select the outlier or reference result row used for a representative plot."""

    if not window_rows:
        return None

    selected = next((row for row in window_rows if row.get("is_outlier")), None)
    selection_reason = "outlier_window"

    if selected is None and plot_metadata is not None:
        reference_index = plot_metadata.get("reference_window_index")
        if reference_index is not None:
            reference_window_number = int(reference_index) + 1
            selected = next(
                (
                    row
                    for row in window_rows
                    if int(row.get("window_index", -1)) == reference_window_number
                ),
                None,
            )
            selection_reason = "reference_window"

    if selected is None:
        selected = window_rows[0]
        selection_reason = "first_window"

    return {"selection_reason": selection_reason, **selected}


def write_result_json(
    *,
    profile,
    out_dir: Path,
    artifact_stem: str,
    test_name: str,
    config_name: str,
    config_path: Path,
    config_data: dict,
    result_paths: dict,
    measured_at: str,
    campaign_id: str,
    supply_v: float | None,
    retain_capture: bool = False,
) -> Path:
    if config_path is None:
        raise RuntimeError("Missing result context config_path for result JSON generation")
    config_path = Path(config_path)
    campaign_id = campaign_id or measured_at
    result_json_path = out_dir / f"{artifact_stem}_result.json"
    capture_policy = _capture_policy(config_path)
    if retain_capture:
        capture_policy = {**capture_policy, "retained": True}
    capture_path = _save_capture_npz(profile, out_dir, artifact_stem) if capture_policy["retained"] else None
    firmware = config_data.get("firmware", {})
    bench = config_data.get("bench", {})
    advertiser = config_data.get("advertiser", {})
    scanner = config_data.get("scanner", {})
    connection = config_data.get("connection", {})
    pawr = config_data.get("pawr", {})
    slcp_meta = _read_slcp_metadata(firmware.get("dut_slcp"))
    sdk_name = firmware.get("sisdk_name") or slcp_meta["sdk_name"]
    sdk_version = firmware.get("sisdk_version") or slcp_meta["sdk_version"]
    mode, partition = _config_mode_and_partition(config_path)
    analyzer = result_paths["analyzer"] if result_paths["note_path"] is None else None
    storage = _result_storage_metadata(
        test_name=test_name,
        config_name=config_name,
        config_path=config_path,
        firmware=firmware,
        bench=bench,
        slcp_meta=slcp_meta,
    )

    avg_current_ma = float(sum(profile["current"]["samples"]) / len(profile["current"]["samples"]) * 1e3)
    peak_current_ma = float(max(profile["current"]["samples"]) * 1e3)
    rx_current_ma = _current_mean_ma_from_label(analyzer, "RX") if analyzer is not None else None
    tx_current_ma = _current_mean_ma_from_label(analyzer, "TX") if analyzer is not None else None

    artifact_refs = [
        {
            "artifact_type": "config_snapshot",
            "path": _relative_path(config_path),
            "sha256": _file_sha256(config_path),
        }
    ]
    for artifact_type, path in (
        ("capture_npz", capture_path),
        ("consolidated_csv", result_paths["consolidated_csv"]),
        ("deep_dive_csv", result_paths["deep_dive_csv"]),
        ("html_report", result_paths["html_report"]),
        ("analysis_note", result_paths["note_path"]),
    ):
        if path is None:
            continue
        artifact_refs.append(
            {
                "artifact_type": artifact_type,
                "path": _relative_path(path),
                "sha256": _file_sha256(Path(path)),
            }
        )

    record = {
        "result_type": ResultType.CURRENT_MEASUREMENT.value,
        "schema_version": 1,
        "dataset": {
            "dataset_id": f"ble_{firmware.get('board', 'unknown')}_{campaign_id}",
            "dataset_name": f"ble_{firmware.get('board', 'unknown')}",
            "wireless_stack": "BLE",
            "chip_id": slcp_meta["chip_id"],
            "board_id": firmware.get("board"),
            "board_revision": storage["board_revision"],
            "part_number": storage["part_number"],
            "sdk_name": sdk_name,
            "sdk_version": sdk_version,
            "campaign_id": campaign_id,
            "mode": mode,
            "partition": partition,
        },
        "storage": storage,
        "test_run": {
            "test_name": test_name,
            "config_name": config_name,
            "config_path": _relative_path(config_path),
            "config_hash": _file_sha256(config_path),
            "status": "capture_only" if result_paths["note_path"] is not None else "analyzed",
            "measured_at": measured_at,
            "capture_retained": capture_policy["retained"],
            "capture_policy": capture_policy,
        },
        "firmware": {
            "dut_slcp": firmware.get("dut_slcp"),
            "tester_slcp": firmware.get("tester_slcp"),
            "board": firmware.get("board"),
            "board_revision": storage["board_revision"],
            "brev": firmware.get("brev"),
            "part_number": storage["part_number"],
            "device": firmware.get("device"),
            "serial_number": storage["serial_number"],
            "dut_sdk_name": sdk_name,
            "dut_sdk_version": sdk_version,
            "dut_sdk_source": firmware.get("sisdk_source"),
            "dut_sdk_commit": firmware.get("sisdk_commit"),
            "dut_sdk_hash": firmware.get("sisdk_hash"),
            "dut_xapi_sha256": firmware.get("xapi_file_hash"),
            "dut_chip_id": slcp_meta["chip_id"],
            "tester_sdk_name": None,
            "tester_sdk_version": None,
            "tester_chip_id": None,
        },
        "parameters": {
            "supply_voltage_v": supply_v,
            "phy": _result_phy(test_name, advertiser, scanner, connection),
            "tx_power_dbm": advertiser.get("tx_power") or connection.get("tx_power"),
            "advertising_interval_ms": advertiser.get("interval"),
            "tx_payload_length_bytes": advertiser.get("payload") or connection.get("tx_payload_length"),
            "rx_payload_length_bytes": connection.get("rx_payload_length") or pawr.get("rx_payload_length"),
        },
        "result_metric": {
            "avg_current_ma": avg_current_ma,
            "peak_current_ma": peak_current_ma,
            "rx_current_ma": rx_current_ma,
            "tx_current_ma": tx_current_ma,
            "cpu_current_ma": None,
            "metric_status": {
                "avg_current_ma": "measured_from_global_capture",
                "peak_current_ma": "measured_from_capture_peak",
                "rx_current_ma": "derived_from_analyzer_label" if rx_current_ma is not None else "unavailable",
                "tx_current_ma": "derived_from_analyzer_label" if tx_current_ma is not None else "unavailable",
                "cpu_current_ma": "unavailable",
            },
        },
        "result_summary": _build_result_summary(analyzer) if analyzer is not None else [],
        "test_result_window": _build_window_rows(analyzer) if analyzer is not None else [],
        "artifact_ref": artifact_refs,
    }
    result_json_path.write_text(json.dumps(_json_ready(record), indent=2) + "\n", encoding="utf-8")
    return result_json_path


class ResultGenerator:
    def __init__(
        self,
        *,
        campaign_id: str | None = None,
        upload_results_to_database: bool | None = None,
        retain_captures_for_debug: bool = False,
    ):
        self.context = ResultContext(
            campaign_id=campaign_id
            or os.environ.get("CURRENT_MEASUREMENT_CAMPAIGN_ID")
            or utc_now().replace(":", "").replace("-", "")
        )
        self.upload_results_to_database = (
            _upload_results_to_database_from_env()
            if upload_results_to_database is None
            else upload_results_to_database
        )
        self.retain_captures_for_debug = retain_captures_for_debug

    def set_context(
        self,
        *,
        test_name: str,
        config_name: str,
        config_path: Path,
        config_data: dict,
        supply_v: float | None,
    ) -> None:
        self.context.test_name = test_name
        self.context.config_name = config_name
        self.context.config_path = config_path
        self.context.config_data = config_data
        self.context.supply_v = supply_v

    def write(
        self,
        *,
        profile,
        out_dir: Path,
        artifact_stem: str,
        result_paths: dict,
        measured_at: str | None = None,
    ) -> Path:    
        effective_measured_at = measured_at or utc_now()
        status_path = _upload_status_path(out_dir, artifact_stem)
        status_path.parent.mkdir(parents=True, exist_ok=True)
        result_json_path = None
        png_path = None
        phase = "result_json"
        _write_upload_status(status_path, status="running", phase=phase)
        try:
            result_json_path = write_result_json(
                profile=profile,
                out_dir=out_dir,
                artifact_stem=artifact_stem,
                test_name=self.context.test_name,
                config_name=self.context.config_name,
                config_path=self.context.config_path,
                config_data=self.context.config_data,
                result_paths=result_paths,
                measured_at=effective_measured_at,
                campaign_id=self.context.campaign_id,
                supply_v=self.context.supply_v,
                retain_capture=self.retain_captures_for_debug,
            )
            log.info("Result JSON written: %s", result_json_path)
            _write_upload_status(
                status_path,
                status="running" if self.upload_results_to_database else "disabled",
                phase="render_png" if self.upload_results_to_database else "complete",
                result_json_path=result_json_path,
            )

            capture_path = out_dir / f"{artifact_stem}_capture.npz"
            if self.upload_results_to_database and capture_path.is_file():
                phase = "render_png"
                from utils.render_capture_image import render_capture_image

                png_path = render_capture_image(
                    capture_path,
                    output_path=capture_path.with_suffix(".png"),
                    result_json_path=result_json_path,
                )
                _write_upload_status(
                    status_path,
                    status="running",
                    phase="upload_json",
                    result_json_path=result_json_path,
                    png_path=png_path,
                )

            if self.upload_results_to_database:
                upload_metadata = _upload_metadata(
                    config_path=self.context.config_path,
                    config_data=self.context.config_data,
                    measured_at=effective_measured_at,
                )
                phase = "upload_json"
                _write_upload_status(
                    status_path,
                    status="running",
                    phase=phase,
                    result_json_path=result_json_path,
                    png_path=png_path,
                )
                response = upload_result_json_to_database(result_json_path, **upload_metadata)
                log.info("Result JSON uploaded to database: %s", response)
                if png_path is not None:
                    phase = "upload_png"
                    _write_upload_status(
                        status_path,
                        status="running",
                        phase=phase,
                        result_json_path=result_json_path,
                        png_path=png_path,
                    )
                    response = upload_result_file_to_database(png_path, **upload_metadata)
                    log.info("Result PNG uploaded to database: %s", response)
                _write_upload_status(
                    status_path,
                    status="uploaded",
                    phase="complete",
                    result_json_path=result_json_path,
                    png_path=png_path,
                )
            return result_json_path
        except Exception as exc:
            _write_upload_status(
                status_path,
                status="failed",
                phase=phase,
                result_json_path=result_json_path,
                png_path=png_path,
                error=f"{type(exc).__name__}: {exc}",
            )
            log.exception("Result processing failed during %s", phase)
            raise

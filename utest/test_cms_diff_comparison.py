import json
from pathlib import Path

import pytest

from cms_diff_comparison import (
    DifferentialPairingError,
    PercentageThresholdPolicy,
    ResultMetricParseError,
    create_diff_comparison_report,
    extract_measurement_metrics,
    pair_downloaded_differential_results,
    prepare_comparison_inputs,
    render_diff_comparison_table,
    write_diff_comparison_report,
)


def _write_plan(path: Path, *, role: str = "dcdc", partition: str = "") -> None:
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "feature": "beacon",
                        "config_path": "tests/configs/ble/beacon/default.json",
                        "config_hash": "config-sha256",
                        "partition": partition,
                        "mode": "default",
                        "firmware_role": role,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _write_result(root: Path, filename: str, *, config_hash: str = "config-sha256") -> Path:
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "test_run": {
                    "test_name": "beacon",
                    "config_path": "tests\\configs\\ble\\beacon\\default.json",
                    "config_hash": config_hash,
                },
                "dataset": {"mode": "default", "partition": ""},
                "firmware": {"board": "brd4182a", "device": "EFR32MG22"},
                "result_metric": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_pairs_version1_and_version2_by_stable_identity_not_filename_or_order(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    _write_result(version1_root, "arbitrary-version1-name.json")
    _write_result(version2_root, "different-version2-name.json")

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)

    assert len(pairing.pairs) == 1
    assert pairing.pairs[0].identity.firmware_role == "dcdc"
    assert pairing.pairs[0].version1.variant == "version1"
    assert pairing.pairs[0].version2.variant == "version2"
    assert not pairing.unmatched_version1
    assert not pairing.unmatched_version2


def test_pairs_absolute_plan_config_paths_with_repo_relative_result_paths(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["jobs"][0]["config_path"] = "/home/admin/current_measurement_tool/tests/configs/ble/beacon/default.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    _write_result(version1_root, "version1.json")
    _write_result(version2_root, "version2.json")

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)

    assert len(pairing.pairs) == 1
    assert pairing.pairs[0].identity.config_path == "tests/configs/ble/beacon/default.json"


def test_resolves_missing_result_partition_from_the_paired_plan(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path, partition="dcdc")
    version1_path = _write_result(version1_root, "version1.json")
    version2_path = _write_result(version2_root, "version2.json")
    for path in (version1_path, version2_path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["dataset"]["partition"]
        path.write_text(json.dumps(payload), encoding="utf-8")

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)

    assert len(pairing.pairs) == 1
    assert pairing.pairs[0].identity.partition == "dcdc"


def test_retains_a_missing_version2_as_an_unmatched_version1_result(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    version2_root.mkdir(parents=True)
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path, role="no_dcdc")
    _write_result(version1_root, "version1-only.json")

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)

    assert not pairing.pairs
    assert len(pairing.unmatched_version1) == 1
    assert pairing.unmatched_version1[0].identity.firmware_role == "no_dcdc"
    assert not pairing.unmatched_version2


def test_rejects_duplicate_variant_results_for_one_stable_identity(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    version2_root.mkdir(parents=True)
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    _write_result(version1_root, "first.json")
    _write_result(version1_root, "second.json")

    with pytest.raises(DifferentialPairingError, match="duplicate result identity"):
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)


def test_extracts_current_metrics_and_interval_sleep_current() -> None:
    metrics = extract_measurement_metrics(
        {
            "result_metric": {
                "avg_current_ma": 1.25,
                "peak_current_ma": 3,
                "rx_current_ma": 2.5,
                "tx_current_ma": 2.75,
            },
            "result_summary": [
                {"meas_type": "Event", "i_mean_ma": 9.0},
                {"meas_type": "Interval", "i_mean_ma": 0.125},
            ],
        }
    )

    assert metrics.avg_current_ma == 1.25
    assert metrics.peak_current_ma == 3.0
    assert metrics.rx_current_ma == 2.5
    assert metrics.tx_current_ma == 2.75
    assert metrics.sleep_current_ma == 0.125


def test_allows_unavailable_metrics_and_missing_interval_summary() -> None:
    metrics = extract_measurement_metrics(
        {
            "result_metric": {
                "avg_current_ma": 1.25,
                "peak_current_ma": None,
                "rx_current_ma": None,
                "tx_current_ma": None,
            },
            "result_summary": [],
        }
    )

    assert metrics.avg_current_ma == 1.25
    assert metrics.peak_current_ma is None
    assert metrics.rx_current_ma is None
    assert metrics.tx_current_ma is None
    assert metrics.sleep_current_ma is None


def test_rejects_malformed_metric_values_and_ambiguous_interval_summary() -> None:
    with pytest.raises(ResultMetricParseError, match="avg_current_ma"):
        extract_measurement_metrics(
            {
                "result_metric": {
                    "avg_current_ma": "1.25",
                    "peak_current_ma": 2.0,
                    "rx_current_ma": None,
                    "tx_current_ma": None,
                }
            }
        )


def test_prepares_only_available_metrics_and_preserves_json_ready_omission_reasons(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    version1_path = _write_result(version1_root, "version1.json")
    version2_path = _write_result(version2_root, "version2.json")
    for path, rx_value in ((version1_path, 2.0), (version2_path, None)):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["result_metric"] = {
            "avg_current_ma": 1.0,
            "peak_current_ma": None,
            "rx_current_ma": rx_value,
            "tx_current_ma": 3.0,
            "metric_status": {"rx_current_ma": "unavailable" if rx_value is None else "measured"},
        }
        payload["result_summary"] = []
        path.write_text(json.dumps(payload), encoding="utf-8")

    inputs = prepare_comparison_inputs(
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)
    )

    assert [metric.variable for metric in inputs.metrics] == ["avg_current_ma", "tx_current_ma"]
    diagnostics = [diagnostic.to_dict() for diagnostic in inputs.diagnostics]
    assert {item["code"] for item in diagnostics} == {
        "null_metric",
        "marked_unavailable",
        "missing_interval_summary",
    }
    assert all("identity" in item for item in diagnostics)


def test_retains_unmatched_result_diagnostics(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    version2_root.mkdir(parents=True)
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    version1_path = _write_result(version1_root, "version1.json")
    payload = json.loads(version1_path.read_text(encoding="utf-8"))
    payload["result_metric"] = {
        "avg_current_ma": "not-a-number",
        "peak_current_ma": 2.0,
        "rx_current_ma": None,
        "tx_current_ma": None,
    }
    version1_path.write_text(json.dumps(payload), encoding="utf-8")

    inputs = prepare_comparison_inputs(
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)
    )

    assert not inputs.metrics
    assert [diagnostic.code for diagnostic in inputs.diagnostics] == ["unmatched_version1_result"]


def test_retains_malformed_result_diagnostics_for_a_matched_pair(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    version1_path = _write_result(version1_root, "version1.json")
    _write_result(version2_root, "version2.json")
    payload = json.loads(version1_path.read_text(encoding="utf-8"))
    payload["result_metric"] = {
        "avg_current_ma": "not-a-number",
        "peak_current_ma": 2.0,
        "rx_current_ma": None,
        "tx_current_ma": None,
    }
    version1_path.write_text(json.dumps(payload), encoding="utf-8")

    inputs = prepare_comparison_inputs(
        pair_downloaded_differential_results(version1_root, version2_root, plan_path)
    )

    assert not inputs.metrics
    assert [diagnostic.code for diagnostic in inputs.diagnostics] == ["malformed_result_metric"]


def test_creates_versioned_report_with_percent_statuses_and_absolute_deltas(tmp_path: Path) -> None:
    version1_root = tmp_path / "results" / "version1"
    version2_root = tmp_path / "results" / "version2"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    version1_path = _write_result(version1_root, "version1.json")
    version2_path = _write_result(version2_root, "version2.json")
    for path, values in (
        (version1_path, {"avg": 1.0, "peak": 1.0, "rx": 1.0, "tx": 1.0, "sleep": 0.0}),
        (version2_path, {"avg": 1.016, "peak": 1.02, "rx": 0.99, "tx": 1.0, "sleep": 0.0}),
    ):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["result_metric"] = {
            "avg_current_ma": values["avg"],
            "peak_current_ma": values["peak"],
            "rx_current_ma": values["rx"],
            "tx_current_ma": values["tx"],
        }
        payload["result_summary"] = [{"meas_type": "Interval", "i_mean_ma": values["sleep"]}]
        path.write_text(json.dumps(payload), encoding="utf-8")

    pairing = pair_downloaded_differential_results(version1_root, version2_root, plan_path)
    report = create_diff_comparison_report(
        pairing,
        prepare_comparison_inputs(pairing),
        thresholds=PercentageThresholdPolicy(warning_percent=1.5, failure_percent=2.0),
    )
    saved_path = write_diff_comparison_report(report, tmp_path / "diff_comparison.json")

    metrics = {item["variable"]: item for item in report["pairs"][0]["metrics"]}
    assert report["schema_version"] == 1
    assert metrics["avg_current_ma"]["delta"] == pytest.approx(0.016)
    assert metrics["avg_current_ma"]["percent"] == pytest.approx(1.6)
    assert metrics["avg_current_ma"]["status"] == "WARNING"
    assert metrics["peak_current_ma"]["status"] == "FAIL"
    assert metrics["rx_current_ma"]["status"] == "PASS"
    assert metrics["sleep_current_ma"]["percent"] is None
    assert metrics["sleep_current_ma"]["status"] == "UNAVAILABLE"
    assert any(item["code"] == "zero_version1_baseline" for item in report["diagnostics"])
    assert json.loads(saved_path.read_text(encoding="utf-8"))["pairs"] == report["pairs"]


def test_renders_requested_markdown_table_from_report_only(tmp_path: Path) -> None:
    report = {
        "pairs": [
            {
                "identity": {
                    "feature": "beacon",
                    "config_path": "tests/configs/ble/beacon/default.json",
                    "firmware_role": "dcdc",
                },
                "version1": {"provenance": {"sisdk_version": "2025.12.3"}},
                "version2": {"provenance": {"sisdk_version": "2025.12.2"}},
                "metrics": [
                    {"variable": "tx_current_ma", "version2": 1.016, "version1": 1.0, "delta": 0.016, "percent": 1.6, "status": "WARNING"},
                    {"variable": "avg_current_ma", "version2": 0.5, "version1": 0.5, "delta": 0.0, "percent": 0.0, "status": "PASS"},
                    {"variable": "sleep_current_ma", "version2": 0.0, "version1": 0.0, "delta": 0.0, "percent": None, "status": "UNAVAILABLE"},
                ],
            }
        ]
    }

    table = render_diff_comparison_table(report)

    assert "SiSDK 2025.12.2" in table
    assert "SiSDK 2025.12.3" in table
    assert "Metric [mA]" in table
    assert "avg" in table
    assert "+0.016" in table
    assert "+1.60%" in table
    assert "! WARN" in table
    assert "peak_current_ma" not in table
    assert table.index("avg") < table.index("tx") < table.index("sleep")
    with pytest.raises(ResultMetricParseError, match="multiple Interval"):
        extract_measurement_metrics(
            {
                "result_metric": {
                    "avg_current_ma": 1.0,
                    "peak_current_ma": 2.0,
                    "rx_current_ma": None,
                    "tx_current_ma": None,
                },
                "result_summary": [
                    {"meas_type": "Interval", "i_mean_ma": 0.1},
                    {"meas_type": "Interval", "i_mean_ma": 0.2},
                ],
            }
        )


def _normalized_report_record(
    feature: str,
    status: str | None,
    *,
    reason: str | None = None,
    firmware_role: str = "dcdc",
    version2_missing: bool = False,
) -> dict:
    version1_value = 1.0
    version2_value = None if version2_missing else (1.02 if status == "FAIL" else 1.01)

    def variant(name: str, value: float | None, *, missing: bool = False) -> dict:
        version = "2026.6.1" if name == "version1" else "2026.6.2"
        build = "101" if name == "version1" else "202"
        avg_available = value is not None
        return {
            "execution_status": "missing_result" if missing else "analyzed",
            "measured_at": f"2026-11-20T10:0{'1' if name == 'version1' else '2'}:00Z",
            "cms_bench_id": f"BENCH-{build}",
            "provenance": {
                "dut_sdk_version": version,
                "dut_sdk_artifactory_build": build,
            },
            "metrics": {
                "avg_current_ma": {
                    "value": value,
                    "available": avg_available,
                    "availability_reason": None if avg_available else "missing_result",
                },
                "peak_current_ma": {
                    "value": None,
                    "available": False,
                    "availability_reason": "not_measured" if not missing else "missing_result",
                },
            },
        }

    version1 = variant("version1", version1_value)
    version2 = variant("version2", version2_value, missing=version2_missing)
    comparisons = []
    if version2_value is not None:
        delta = version2_value - version1_value
        percent = delta / version1_value * 100
        comparisons.append(
            {
                "variable": "avg_current_ma",
                "version1": version1_value,
                "version2": version2_value,
                "delta": delta,
                "percent": percent,
                "status": "FAIL" if percent >= 2 else "PASS",
            }
        )

    return {
        "identity": {
            "feature": feature,
            "config_path": f"tests/configs/ble/{feature}/default.json",
            "config_hash": f"sha-{feature}",
            "partition": firmware_role,
            "mode": "default",
            "board": "BRD4182A",
            "device": "EFR32MG22",
            "firmware_role": firmware_role,
            "supply_voltage_v": 3.3,
        },
        "version1": version1,
        "version2": version2,
        "metrics": comparisons,
        "differential_status": status,
        "reason": reason,
    }


def test_renders_normalized_record_metadata_metrics_and_reasons() -> None:
    report = {
        "records": [
            _normalized_report_record(
                "beacon",
                "FAIL",
                reason="avg_current_ma regression (+2.00%)",
            ),
            _normalized_report_record(
                "connection",
                "INCOMPLETE",
                reason="no version2 result has the same stable identity",
                firmware_role="no_dcdc",
                version2_missing=True,
            ),
        ]
    }

    output = render_diff_comparison_table(report)

    assert "DCDC mode: DCDC" in output
    assert "DCDC mode: NO_DCDC" in output
    assert "Config hash: sha-beacon" in output
    assert "Board: BRD4182A" in output
    assert "SiSDK 2026.6.1" in output
    assert "Build 101" in output
    assert "CMS Bench BENCH-101" in output
    assert "status analyzed" in output
    assert "Differential result: FAIL" in output
    assert "avg_current_ma regression (+2.00%)" in output
    assert "+0.02" in output
    assert "+2.00%" in output
    assert "Differential result: INCOMPLETE" in output
    assert "no version2 result has the same stable identity" in output
    assert "V2: missing_result" in output


def test_renders_final_summary_from_normalized_test_statuses() -> None:
    report = {
        "records": [
            _normalized_report_record("pass_case", "PASS"),
            _normalized_report_record(
                "fail_case", "FAIL", reason="metric regression"
            ),
            _normalized_report_record(
                "incomplete_case",
                "INCOMPLETE",
                reason="missing version2 result",
                version2_missing=True,
            ),
            _normalized_report_record(
                "unknown_case", None, reason="classification not recorded"
            ),
        ]
    }

    output = render_diff_comparison_table(report)

    assert output.endswith(
        "Differential summary: Passed 1 | Failed 1 | Incomplete 2"
    )
    assert "Differential result: INCOMPLETE" in output

    empty_output = render_diff_comparison_table({"records": []})
    assert empty_output == (
        "No comparable metrics.\n\n"
        "Differential summary: Passed 0 | Failed 0 | Incomplete 0"
    )

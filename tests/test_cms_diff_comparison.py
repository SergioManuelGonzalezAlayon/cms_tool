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


def _write_plan(path: Path, *, role: str = "dcdc") -> None:
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "feature": "beacon",
                        "config_path": "tests/configs/ble/beacon/default.json",
                        "config_hash": "config-sha256",
                        "partition": "",
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


def test_pairs_parent_and_child_by_stable_identity_not_filename_or_order(tmp_path: Path) -> None:
    parent_root = tmp_path / "results" / "parent"
    child_root = tmp_path / "results" / "child"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    _write_result(parent_root, "arbitrary-parent-name.json")
    _write_result(child_root, "different-child-name.json")

    pairing = pair_downloaded_differential_results(parent_root, child_root, plan_path)

    assert len(pairing.pairs) == 1
    assert pairing.pairs[0].identity.firmware_role == "dcdc"
    assert pairing.pairs[0].parent.variant == "parent"
    assert pairing.pairs[0].child.variant == "child"
    assert not pairing.unmatched_parent
    assert not pairing.unmatched_child


def test_retains_a_missing_child_as_an_unmatched_parent_result(tmp_path: Path) -> None:
    parent_root = tmp_path / "results" / "parent"
    child_root = tmp_path / "results" / "child"
    child_root.mkdir(parents=True)
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path, role="no_dcdc")
    _write_result(parent_root, "parent-only.json")

    pairing = pair_downloaded_differential_results(parent_root, child_root, plan_path)

    assert not pairing.pairs
    assert len(pairing.unmatched_parent) == 1
    assert pairing.unmatched_parent[0].identity.firmware_role == "no_dcdc"
    assert not pairing.unmatched_child


def test_rejects_duplicate_variant_results_for_one_stable_identity(tmp_path: Path) -> None:
    parent_root = tmp_path / "results" / "parent"
    child_root = tmp_path / "results" / "child"
    child_root.mkdir(parents=True)
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    _write_result(parent_root, "first.json")
    _write_result(parent_root, "second.json")

    with pytest.raises(DifferentialPairingError, match="duplicate result identity"):
        pair_downloaded_differential_results(parent_root, child_root, plan_path)


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
    parent_root = tmp_path / "results" / "parent"
    child_root = tmp_path / "results" / "child"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    parent_path = _write_result(parent_root, "parent.json")
    child_path = _write_result(child_root, "child.json")
    for path, rx_value in ((parent_path, 2.0), (child_path, None)):
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
        pair_downloaded_differential_results(parent_root, child_root, plan_path)
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
    parent_root = tmp_path / "results" / "parent"
    child_root = tmp_path / "results" / "child"
    child_root.mkdir(parents=True)
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    parent_path = _write_result(parent_root, "parent.json")
    payload = json.loads(parent_path.read_text(encoding="utf-8"))
    payload["result_metric"] = {
        "avg_current_ma": "not-a-number",
        "peak_current_ma": 2.0,
        "rx_current_ma": None,
        "tx_current_ma": None,
    }
    parent_path.write_text(json.dumps(payload), encoding="utf-8")

    inputs = prepare_comparison_inputs(
        pair_downloaded_differential_results(parent_root, child_root, plan_path)
    )

    assert not inputs.metrics
    assert [diagnostic.code for diagnostic in inputs.diagnostics] == ["unmatched_parent_result"]


def test_retains_malformed_result_diagnostics_for_a_matched_pair(tmp_path: Path) -> None:
    parent_root = tmp_path / "results" / "parent"
    child_root = tmp_path / "results" / "child"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    parent_path = _write_result(parent_root, "parent.json")
    _write_result(child_root, "child.json")
    payload = json.loads(parent_path.read_text(encoding="utf-8"))
    payload["result_metric"] = {
        "avg_current_ma": "not-a-number",
        "peak_current_ma": 2.0,
        "rx_current_ma": None,
        "tx_current_ma": None,
    }
    parent_path.write_text(json.dumps(payload), encoding="utf-8")

    inputs = prepare_comparison_inputs(
        pair_downloaded_differential_results(parent_root, child_root, plan_path)
    )

    assert not inputs.metrics
    assert [diagnostic.code for diagnostic in inputs.diagnostics] == ["malformed_result_metric"]


def test_creates_versioned_report_with_percent_statuses_and_absolute_deltas(tmp_path: Path) -> None:
    parent_root = tmp_path / "results" / "parent"
    child_root = tmp_path / "results" / "child"
    plan_path = tmp_path / "paired_default_plan.json"
    _write_plan(plan_path)
    parent_path = _write_result(parent_root, "parent.json")
    child_path = _write_result(child_root, "child.json")
    for path, values in (
        (parent_path, {"avg": 1.0, "peak": 1.0, "rx": 1.0, "tx": 1.0, "sleep": 0.0}),
        (child_path, {"avg": 1.016, "peak": 1.02, "rx": 0.99, "tx": 1.0, "sleep": 0.0}),
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

    pairing = pair_downloaded_differential_results(parent_root, child_root, plan_path)
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
    assert any(item["code"] == "zero_parent_baseline" for item in report["diagnostics"])
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
                "metrics": [
                    {"variable": "tx_current_ma", "child": 1.016, "parent": 1.0, "delta": 0.016, "percent": 1.6, "status": "WARNING"},
                    {"variable": "avg_current_ma", "child": 0.5, "parent": 0.5, "delta": 0.0, "percent": 0.0, "status": "PASS"},
                    {"variable": "sleep_current_ma", "child": 0.0, "parent": 0.0, "delta": 0.0, "percent": None, "status": "UNAVAILABLE"},
                ],
            }
        ]
    }

    table = render_diff_comparison_table(report)

    assert "| Variable | Child | Parent | Delta (child-parent) | Percent | Status |" in table
    assert "| avg_current_ma | 0.5 | 0.5 | 0 | 0.000% | PASS |" in table
    assert "| tx_current_ma | 1.016 | 1 | 0.016 | 1.600% | WARNING |" in table
    assert "| sleep_current_ma | 0 | 0 | 0 | N/A | UNAVAILABLE |" in table
    assert "peak_current_ma" not in table
    assert table.index("avg_current_ma") < table.index("tx_current_ma") < table.index("sleep_current_ma")
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

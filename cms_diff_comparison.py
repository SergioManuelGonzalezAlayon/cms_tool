"""Stable version1/version2 result pairing for CMS differential campaigns.

This module intentionally performs no network or CLI work.  It consumes the
already downloaded ``results/version1`` and ``results/version2`` artifact roots,
then uses the retained paired job plan to supply the explicit partition and
DUT firmware role for every raw measurement result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import math
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.filters import AutoFilter
from openpyxl.worksheet.table import Table, TableStyleInfo


class DifferentialPairingError(ValueError):
    """Raised when retained campaign evidence cannot be paired safely."""


class ResultMetricParseError(ValueError):
    """Raised when a raw measurement artifact has an invalid metric schema."""


class ComparisonStatus(str, Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class PairingIdentity:
    """Stable identity for one version1/version2 measurement pair."""

    feature: str
    config_path: str
    config_hash: str
    partition: str
    mode: str
    board: str
    device: str
    firmware_role: str
    supply_voltage_v: float | None = None


@dataclass(frozen=True, slots=True)
class PlannedJobIdentity:
    """Plan-owned identity values absent from some raw result schemas."""

    partition: str
    firmware_role: str


@dataclass(frozen=True, slots=True)
class VariantResult:
    """One raw result artifact with its resolved differential identity."""

    variant: str
    path: Path
    identity: PairingIdentity
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResultPair:
    """The matching version1 and version2 evidence for one stable identity."""

    identity: PairingIdentity
    version1: VariantResult
    version2: VariantResult


@dataclass(frozen=True, slots=True)
class PairingResult:
    """Complete pairing outcome, including evidence unavailable on one side."""

    pairs: tuple[ResultPair, ...]
    unmatched_version1: tuple[VariantResult, ...]
    unmatched_version2: tuple[VariantResult, ...]


@dataclass(frozen=True, slots=True)
class MeasurementMetrics:
    """Numerical metrics extracted from one raw measurement result artifact."""

    avg_current_ma: float | None
    peak_current_ma: float | None
    rx_current_ma: float | None
    tx_current_ma: float | None
    sleep_current_ma: float | None
    event_energy_nj: float | None
    interval_energy_nj: float | None


@dataclass(frozen=True, slots=True)
class ComparisonMetricInput:
    """One metric with valid values available from both firmware variants."""

    identity: PairingIdentity
    variable: str
    version1_value: float
    version2_value: float


@dataclass(frozen=True, slots=True)
class ComparisonDiagnostic:
    """A JSON-ready reason why evidence cannot participate in comparison."""

    code: str
    message: str
    variant: str = ""
    variable: str = ""
    identity: PairingIdentity | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "variant": self.variant,
            "variable": self.variable,
        }
        if self.identity is not None:
            payload["identity"] = {
                "feature": self.identity.feature,
                "config_path": self.identity.config_path,
                "config_hash": self.identity.config_hash,
                "partition": self.identity.partition,
                "mode": self.identity.mode,
                "board": self.identity.board,
                "device": self.identity.device,
                "firmware_role": self.identity.firmware_role,
                "supply_voltage_v": self.identity.supply_voltage_v,
            }
        return payload


@dataclass(frozen=True, slots=True)
class ComparisonInputs:
    """Comparable values plus retained diagnostics for later JSON reporting."""

    metrics: tuple[ComparisonMetricInput, ...]
    diagnostics: tuple[ComparisonDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class PercentageThresholdPolicy:
    """Initial differential status policy for positive metric regressions."""

    warning_percent: float = 1.5
    failure_percent: float = 2.0

    def __post_init__(self) -> None:
        if self.warning_percent < 0:
            raise ValueError("warning percentage threshold cannot be negative")
        if self.failure_percent < self.warning_percent:
            raise ValueError("failure percentage threshold cannot be below warning threshold")


_METRIC_FIELDS = (
    "avg_current_ma",
    "peak_current_ma",
    "rx_current_ma",
    "tx_current_ma",
    "sleep_current_ma",
    "event_energy_nj",
    "interval_energy_nj",
)

_TABLE_METRIC_ORDER = (
    "avg_current_ma",
    "peak_current_ma",
    "rx_current_ma",
    "tx_current_ma",
    "sleep_current_ma",
    "event_energy_nj",
    "interval_energy_nj",
)

_METRIC_UNITS = {
    "avg_current_ma": "mA",
    "peak_current_ma": "mA",
    "rx_current_ma": "mA",
    "tx_current_ma": "mA",
    "sleep_current_ma": "mA",
    "event_energy_nj": "nJ",
    "interval_energy_nj": "nJ",
}


def extract_measurement_metrics(payload: dict[str, Any]) -> MeasurementMetrics:
    """Extract comparable metrics from the established raw-result schema.

    ``result_metric`` contains average, peak, RX, and TX values.  Sleep
    current is specifically the ``i_mean_ma`` value of the sole
    ``result_summary`` entry whose ``meas_type`` is ``Interval``. Event and
    interval energy use the corresponding ``e_mean_nj`` summary values.
    """

    if not isinstance(payload, dict):
        raise ResultMetricParseError("measurement result must be a JSON object")
    result_metric = payload.get("result_metric")
    if not isinstance(result_metric, dict):
        raise ResultMetricParseError("measurement result has no result_metric object")
    return MeasurementMetrics(
        avg_current_ma=_optional_current(result_metric, "avg_current_ma"),
        peak_current_ma=_optional_current(result_metric, "peak_current_ma"),
        rx_current_ma=_optional_current(result_metric, "rx_current_ma"),
        tx_current_ma=_optional_current(result_metric, "tx_current_ma"),
        sleep_current_ma=_interval_sleep_current(payload),
        event_energy_nj=_summary_metric(payload, "Event", "e_mean_nj"),
        interval_energy_nj=_summary_metric(payload, "Interval", "e_mean_nj"),
    )


def prepare_comparison_inputs(pairing: PairingResult) -> ComparisonInputs:
    """Keep only comparable metrics and retain deterministic omission reasons."""

    diagnostics: list[ComparisonDiagnostic] = []
    for result in pairing.unmatched_version1:
        diagnostics.append(
            ComparisonDiagnostic(
                code="unmatched_version1_result",
                message="no version2 result has the same stable identity",
                variant="version1",
                identity=result.identity,
            )
        )
    for result in pairing.unmatched_version2:
        diagnostics.append(
            ComparisonDiagnostic(
                code="unmatched_version2_result",
                message="no version1 result has the same stable identity",
                variant="version2",
                identity=result.identity,
            )
        )

    metrics: list[ComparisonMetricInput] = []
    for pair in pairing.pairs:
        version1_metrics = _extract_or_diagnose(pair.version1, diagnostics)
        version2_metrics = _extract_or_diagnose(pair.version2, diagnostics)
        if version1_metrics is None or version2_metrics is None:
            continue
        for variable in _METRIC_FIELDS:
            version1_reason = _metric_omission_reason(pair.version1.payload, variable)
            version2_reason = _metric_omission_reason(pair.version2.payload, variable)
            if (
                variable in {"event_energy_nj", "interval_energy_nj"}
                and getattr(version1_metrics, variable) is None
                and getattr(version2_metrics, variable) is None
                and version1_reason == version2_reason
                and version1_reason is not None
                and version1_reason.startswith("missing_")
            ):
                continue
            if version1_reason is not None:
                diagnostics.append(_omission_diagnostic(pair.version1, variable, version1_reason))
            if version2_reason is not None:
                diagnostics.append(_omission_diagnostic(pair.version2, variable, version2_reason))
            if version1_reason is not None or version2_reason is not None:
                continue
            version1_value = getattr(version1_metrics, variable)
            version2_value = getattr(version2_metrics, variable)
            if version1_value is None or version2_value is None:
                raise AssertionError("metric omission reason must exist for absent values")
            metrics.append(
                ComparisonMetricInput(
                    identity=pair.identity,
                    variable=variable,
                    version1_value=version1_value,
                    version2_value=version2_value,
                )
            )
    return ComparisonInputs(metrics=tuple(metrics), diagnostics=tuple(diagnostics))


def create_diff_comparison_report(
    pairing: PairingResult,
    inputs: ComparisonInputs,
    *,
    thresholds: PercentageThresholdPolicy = PercentageThresholdPolicy(),
) -> dict[str, Any]:
    """Build the authoritative JSON report from paired measurements and diagnostics.

    ``pairs`` contains matched V1/V2 results for the existing terminal renderer.
    ``records`` is the normalized per-test collection, including one-sided results,
    for downstream reports such as Excel.
    """

    metrics_by_identity: dict[PairingIdentity, list[ComparisonMetricInput]] = {}
    for metric in inputs.metrics:
        metrics_by_identity.setdefault(metric.identity, []).append(metric)

    diagnostics = list(inputs.diagnostics)
    pairs: list[dict[str, Any]] = []
    records_with_identity: list[tuple[PairingIdentity, dict[str, Any]]] = []
    for pair in pairing.pairs:
        entries: list[dict[str, Any]] = []
        record_diagnostics = [
            diagnostic
            for diagnostic in inputs.diagnostics
            if diagnostic.identity == pair.identity
        ]
        for metric in sorted(metrics_by_identity.get(pair.identity, []), key=lambda item: item.variable):
            percent = _percent_delta(metric.version2_value, metric.version1_value)
            status = _comparison_status(percent, thresholds)
            if percent is None:
                diagnostic = ComparisonDiagnostic(
                    code="zero_version1_baseline",
                    message=(
                        f"{metric.variable} percentage is unavailable because the version1 value is zero"
                    ),
                    variable=metric.variable,
                    identity=metric.identity,
                )
                diagnostics.append(diagnostic)
                record_diagnostics.append(diagnostic)
            entries.append(
                {
                    "variable": metric.variable,
                    "version1": metric.version1_value,
                    "version2": metric.version2_value,
                    "delta": metric.version2_value - metric.version1_value,
                    "percent": percent,
                    "status": status.value,
                    "thresholds": {
                        "basis": "percent_regression",
                        "warning_percent": thresholds.warning_percent,
                        "failure_percent": thresholds.failure_percent,
                    },
                }
            )
        differential_status, reason = _classify_differential_record(
            entries, record_diagnostics
        )
        record = {
            "identity": _identity_to_dict(pair.identity),
            "version1": _report_variant_input(pair.version1),
            "version2": _report_variant_input(pair.version2),
            "metrics": entries,
            "differential_status": differential_status,
            "reason": reason,
            "diagnostics": [diagnostic.to_dict() for diagnostic in record_diagnostics],
        }
        pairs.append(record)
        records_with_identity.append((pair.identity, record))

    for variant, unmatched_results in (
        ("version1", pairing.unmatched_version1),
        ("version2", pairing.unmatched_version2),
    ):
        for result in unmatched_results:
            record_diagnostics = [
                diagnostic
                for diagnostic in inputs.diagnostics
                if diagnostic.identity == result.identity
            ]
            differential_status, reason = _classify_differential_record(
                [], record_diagnostics
            )
            variants = {
                "version1": _missing_report_variant(),
                "version2": _missing_report_variant(),
            }
            variants[variant] = _report_variant_input(result)
            record = {
                "identity": _identity_to_dict(result.identity),
                "version1": variants["version1"],
                "version2": variants["version2"],
                "metrics": [],
                "differential_status": differential_status,
                "reason": reason,
                "diagnostics": [diagnostic.to_dict() for diagnostic in record_diagnostics],
            }
            records_with_identity.append((result.identity, record))

    records = [
        record
        for _, record in sorted(
            records_with_identity, key=lambda item: _identity_sort_key(item[0])
        )
    ]

    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": {
            "basis": "percent_regression",
            "warning_percent": thresholds.warning_percent,
            "failure_percent": thresholds.failure_percent,
        },
        "pairs": pairs,
        "records": records,
        "diagnostics": [diagnostic.to_dict() for diagnostic in diagnostics],
    }


def write_diff_comparison_report(report: dict[str, Any], output_path: Path) -> Path:
    """Persist the authoritative comparison report as versioned JSON."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def write_diff_excel_report(report: dict[str, Any], output_path: Path) -> Path:
    """Write a filterable, terminal-style metric table to a single-sheet workbook.

    Each normalized test identity contributes one row per terminal metric. The
    comparison columns stay together at the left; execution evidence is retained
    in trailing columns for users who need to audit a result.
    """

    records = report.get("records")
    if not isinstance(records, list):
        raise ValueError("diff comparison report must contain a records list")
    identity_fields = (
        "feature",
        "config_path",
        "config_hash",
        "partition",
        "mode",
        "board",
        "device",
        "firmware_role",
        "supply_voltage_v",
    )
    identity_keys: set[tuple[Any, ...]] = set()
    row_keys: set[tuple[tuple[Any, ...], str]] = set()
    headers = (
        "Test",
        "DCDC Mode",
        "Metric",
        "Version 2",
        "Version 1",
        "Δ (V2 − V1)",
        "Change",
        "Result",
        "Test Result",
        "Setup",
        "V1 Run",
        "V2 Run",
        "Reason",
    )

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Differential Results"
    worksheet.append(headers)
    worksheet.sheet_view.showGridLines = False
    for cell in worksheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(fill_type="solid", fgColor="243746")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    worksheet.row_dimensions[1].height = 30

    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("identity"), dict):
            raise ValueError("diff comparison report contains an invalid normalized record")
        identity = record["identity"]
        identity_key = tuple(identity.get(field) for field in identity_fields)
        if identity_key in identity_keys:
            raise ValueError("diff comparison report contains a duplicate test identity")
        identity_keys.add(identity_key)
        variants = {
            name: record.get(name) if isinstance(record.get(name), dict) else {}
            for name in ("version1", "version2")
        }
        config_path = str(identity.get("config_path") or "")
        config_name = Path(config_path).name if config_path else "N/A"
        test_name = str(identity.get("feature") or "Unknown test")
        if config_name and config_name != "N/A":
            test_name = f"{test_name} / {config_name}"
        role = str(identity.get("firmware_role") or "UNKNOWN").upper()
        run_details: dict[str, str] = {}
        for variant_name in ("version1", "version2"):
            variant = variants[variant_name]
            provenance = variant.get("provenance")
            if not isinstance(provenance, dict):
                provenance = {}
            sdk = provenance.get("sisdk")
            sdk_version = provenance.get("dut_sdk_version") or provenance.get("sisdk_version")
            if not sdk_version and isinstance(sdk, dict):
                sdk_version = sdk.get("version")
            sdk_build = (
                provenance.get("dut_sdk_artifactory_build")
                or provenance.get("sisdk_artifactory_build")
                or provenance.get("build_number")
            )
            run_parts = [f"SiSDK {sdk_version}" if sdk_version else "SiSDK N/A"]
            if sdk_build:
                run_parts.append(f"Build {sdk_build}")
            run_parts.append(str(variant.get("execution_status") or "missing_result"))
            if variant.get("cms_bench_id"):
                run_parts.append(f"Bench {variant['cms_bench_id']}")
            if variant.get("measured_at"):
                run_parts.append(str(variant["measured_at"]))
            run_details[variant_name] = " · ".join(run_parts)

        supply_voltage = identity.get("supply_voltage_v")
        supply_label = "N/A" if supply_voltage is None else f"{supply_voltage} V"
        setup_parts = [
            str(identity.get("board") or ""),
            str(identity.get("device") or ""),
            supply_label,
            str(identity.get("partition") or ""),
            str(identity.get("mode") or ""),
        ]
        setup = " · ".join(part for part in setup_parts if part)
        comparisons = {
            metric.get("variable"): metric
            for metric in record.get("metrics", [])
            if isinstance(metric, dict)
        }
        for variable in _TABLE_METRIC_ORDER:
            row_key = (identity_key, variable)
            if row_key in row_keys:
                raise ValueError("diff comparison report contains a duplicate test metric")
            row_keys.add(row_key)

            observations: dict[str, dict[str, Any]] = {}
            reasons: list[str] = []
            record_reason = record.get("reason")
            if record_reason:
                reasons.append(str(record_reason))
            for variant_name, label in (("version1", "V1"), ("version2", "V2")):
                variant_metrics = variants[variant_name].get("metrics")
                observation = (
                    variant_metrics.get(variable)
                    if isinstance(variant_metrics, dict)
                    else None
                )
                observations[variant_name] = (
                    observation if isinstance(observation, dict) else {}
                )
                if not (
                    observations[variant_name].get("available")
                    and observations[variant_name].get("value") is not None
                ):
                    reason = (
                        observations[variant_name].get("availability_reason")
                    )
                    reasons.append(f"{label}: {reason or 'metric_value_unavailable'}")

            comparison = comparisons.get(variable, {})
            metric_status = comparison.get("status") or "UNAVAILABLE"
            row = [
                test_name,
                role,
                _format_metric_name(variable, include_unit=True),
                observations["version2"].get("value")
                if observations["version2"].get("available")
                and observations["version2"].get("value") is not None
                else "N/A",
                observations["version1"].get("value")
                if observations["version1"].get("available")
                and observations["version1"].get("value") is not None
                else "N/A",
                comparison.get("delta")
                if comparison.get("delta") is not None
                else "N/A",
                comparison.get("percent")
                if comparison.get("percent") is not None
                else "N/A",
                metric_status,
                record.get("differential_status", "INCOMPLETE"),
                setup,
                run_details["version1"],
                run_details["version2"],
                "; ".join(dict.fromkeys(reasons)),
            ]
            worksheet.append(row)
            for cell in worksheet[worksheet.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = "s"

    worksheet.freeze_panes = "D2"
    column_widths = {
        "A": 32,
        "B": 13,
        "C": 22,
        "D": 15,
        "E": 15,
        "F": 17,
        "G": 12,
        "H": 13,
        "I": 16,
        "J": 30,
        "K": 36,
        "L": 36,
        "M": 42,
    }
    for column, width in column_widths.items():
        worksheet.column_dimensions[column].width = width
    worksheet.column_dimensions.group("I", "M", outline_level=1, hidden=True)
    for row in worksheet.iter_rows(min_row=2):
        worksheet.row_dimensions[row[0].row].height = 22
        for cell in row:
            cell.alignment = Alignment(vertical="center")
    for row in range(2, worksheet.max_row + 1):
        for column in (4, 5):
            worksheet.cell(row=row, column=column).number_format = "0.######;-0.######;0"
        worksheet.cell(row=row, column=6).number_format = "+0.######;-0.######;0"
        worksheet.cell(row=row, column=7).number_format = '+0.00"%";-0.00"%";0.00"%"'

    table_ref = worksheet.dimensions
    table = Table(displayName="DifferentialResults", ref=table_ref)
    table.autoFilter = AutoFilter(ref=table_ref)
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    worksheet.add_table(table)
    status_columns = (headers.index("Result") + 1, headers.index("Test Result") + 1)
    if worksheet.max_row > 1:
        for column in status_columns:
            _apply_status_conditional_formatting(worksheet, column)

    if workbook.sheetnames != [worksheet.title]:
        raise ValueError("diff comparison workbook must contain exactly one worksheet")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    _validate_diff_excel_workbook(
        output_path,
        headers,
        len(row_keys),
        status_columns if worksheet.max_row > 1 else (),
    )
    return output_path


def _apply_status_conditional_formatting(worksheet: Any, column: int) -> None:
    """Highlight one result column so metric and test outcomes stand out."""

    if worksheet.max_row < 2:
        return
    column_letter = get_column_letter(column)
    cell_range = f"{column_letter}2:{column_letter}{worksheet.max_row}"
    status_colors = {
        "PASS": "D9EAD3",
        "WARNING": "FFF2CC",
        "FAIL": "F4CCCC",
        "INCOMPLETE": "FCE5CD",
        "UNAVAILABLE": "E7E6E6",
    }
    for status, color in status_colors.items():
        worksheet.conditional_formatting.add(
            cell_range,
            FormulaRule(
                formula=[f'{column_letter}2="{status}"'],
                stopIfTrue=True,
                fill=PatternFill(fill_type="solid", fgColor=color),
            ),
        )


def _validate_diff_excel_workbook(
    output_path: Path,
    expected_headers: tuple[str, ...],
    expected_row_count: int,
    expected_status_columns: tuple[int, ...],
) -> None:
    """Check the saved workbook's table shape, row count, filters, and highlights."""

    workbook = load_workbook(output_path, data_only=True)
    try:
        if workbook.sheetnames != ["Differential Results"]:
            raise ValueError("saved diff comparison workbook must contain exactly one worksheet")
        worksheet = workbook["Differential Results"]
        if worksheet.max_row != expected_row_count + 1:
            raise ValueError("saved diff comparison workbook has an unexpected data row count")
        if worksheet.max_column != len(expected_headers):
            raise ValueError("saved diff comparison workbook has an unexpected column count")
        actual_headers = tuple(
            worksheet.cell(row=1, column=column).value
            for column in range(1, len(expected_headers) + 1)
        )
        if actual_headers != expected_headers:
            raise ValueError("saved diff comparison workbook headers do not match the report")
        if "DifferentialResults" not in worksheet.tables:
            raise ValueError("saved diff comparison workbook is missing its results table")
        table = worksheet.tables["DifferentialResults"]
        if table.ref != worksheet.dimensions:
            raise ValueError("saved diff comparison table does not cover the full report range")
        if table.autoFilter is None or table.autoFilter.ref != worksheet.dimensions:
            raise ValueError("saved diff comparison table is missing its full-range filter")
        if worksheet.freeze_panes != "D2":
            raise ValueError("saved diff comparison workbook is missing frozen headers and identifiers")
        saved_row_keys: set[tuple[Any, ...]] = set()
        for row in worksheet.iter_rows(min_row=2, max_col=10, values_only=True):
            row_key = (row[0], row[1], row[2], row[9])
            if row_key in saved_row_keys:
                raise ValueError("saved diff comparison workbook contains a duplicate test metric")
            saved_row_keys.add(row_key)
        if len(worksheet.conditional_formatting) != len(expected_status_columns):
            raise ValueError("saved diff comparison workbook is missing result highlighting")
        for column in expected_status_columns:
            column_letter = get_column_letter(column)
            expected_range = f"{column_letter}2:{column_letter}{worksheet.max_row}"
            matching_ranges = [
                cell_range
                for cell_range in worksheet.conditional_formatting
                if str(cell_range.sqref) == expected_range
            ]
            if len(matching_ranges) != 1:
                raise ValueError("saved diff comparison workbook has an unexpected highlight range")
            rules = worksheet.conditional_formatting[matching_ranges[0]]
            if len(rules) != 5:
                raise ValueError("saved diff comparison workbook has incomplete status highlights")
    finally:
        workbook.close()


def _render_legacy_diff_comparison_table(report: dict[str, Any]) -> str:
    """Render a terminal-friendly differential comparison table from a JSON report."""

    pairs = report.get("pairs")
    if not isinstance(pairs, list):
        raise ValueError("diff comparison report must contain a pairs list")

    tables: list[str] = []
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("diff comparison report pair must be an object")
        identity = pair.get("identity")
        metrics = pair.get("metrics")
        if not isinstance(identity, dict) or not isinstance(metrics, list):
            raise ValueError("diff comparison report pair has invalid identity or metrics")
        metric_by_name = {
            str(metric.get("variable")): metric
            for metric in metrics
            if isinstance(metric, dict) and str(metric.get("variable", ""))
        }
        rows = [metric_by_name[name] for name in _TABLE_METRIC_ORDER if name in metric_by_name]
        if not rows:
            continue
        include_metric_units = len(_units_for_rows(rows)) > 1
        table_rows = [
            (
                _format_metric_name(
                    str(metric["variable"]), include_unit=include_metric_units
                ),
                _format_number(metric.get("version2")),
                _format_number(metric.get("version1")),
                _format_number(metric.get("delta"), signed=True),
                _format_percent(metric.get("percent")),
                _format_status(str(metric.get("status", "UNAVAILABLE"))),
            )
            for metric in rows
        ]
        tables.extend(
            (
                _table_title(identity),
                _render_terminal_table_with_sisdk(pair, 
                    (_metric_header(rows), "Version 2", "Version 1", "Δ (v2 − v1)", "Change", "Result"),
                    table_rows,
                ),
                "",
            )
        )
    return "\n".join(tables).rstrip() if tables else "No comparable metrics."


def _normalized_differential_summary(records: list[dict[str, Any]]) -> str:
    """Count test-level outcomes from the same records rendered above."""

    counts = {"PASS": 0, "FAIL": 0, "INCOMPLETE": 0}
    for record in records:
        counts[_normalized_record_status(record)] += 1
    return (
        "Differential summary: "
        f"Passed {counts['PASS']} | Failed {counts['FAIL']} | "
        f"Incomplete {counts['INCOMPLETE']}"
    )


def _normalized_record_status(record: dict[str, Any]) -> str:
    """Treat absent or unknown test-level classifications as incomplete."""

    status = record.get("differential_status")
    if isinstance(status, str) and status in {"PASS", "FAIL", "INCOMPLETE"}:
        return status
    return "INCOMPLETE"


def render_diff_comparison_table(report: dict[str, Any]) -> str:
    """Render normalized differential records, including incomplete tests."""

    records = report.get("records")
    if not isinstance(records, list):
        # Preserve support for comparison JSON written before normalized records
        # were added to the report schema.
        return _render_legacy_diff_comparison_table(report)

    tables: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("diff comparison report record must be an object")
        identity = record.get("identity")
        metrics = record.get("metrics")
        if not isinstance(identity, dict) or not isinstance(metrics, list):
            raise ValueError("diff comparison report record has invalid identity or metrics")

        comparisons = {
            str(metric.get("variable")): metric
            for metric in metrics
            if isinstance(metric, dict) and str(metric.get("variable", ""))
        }
        variants = {
            name: record.get(name) if isinstance(record.get(name), dict) else {}
            for name in ("version1", "version2")
        }
        table_rows: list[tuple[str, ...]] = []
        for variable in _TABLE_METRIC_ORDER:
            comparison = comparisons.get(variable, {})
            version2_value, version2_reason = _normalized_metric_observation(
                variants["version2"], variable, comparison, "version2", "V2"
            )
            version1_value, version1_reason = _normalized_metric_observation(
                variants["version1"], variable, comparison, "version1", "V1"
            )
            reasons = [
                reason
                for reason in (version1_reason, version2_reason)
                if reason
            ]
            table_rows.append(
                (
                    _format_metric_name(variable, include_unit=True),
                    version2_value,
                    version1_value,
                    _format_number(comparison.get("delta"), signed=True),
                    _format_percent(comparison.get("percent")),
                    _format_status(str(comparison.get("status", "UNAVAILABLE"))),
                    "; ".join(dict.fromkeys(reasons)),
                )
            )

        tables.extend(
            (
                _normalized_table_title(identity),
                _normalized_identity_setup(identity),
                _normalized_variant_run_line(record, "version1"),
                _normalized_variant_run_line(record, "version2"),
                f"Differential result: {_normalized_record_status(record)}",
            )
        )
        if record.get("reason"):
            tables.append(f"Reason: {record['reason']}")

        version2_header = _normalized_sdk_column_label(record, "version2", "Version 2")
        version1_header = _normalized_sdk_column_label(record, "version1", "Version 1")
        tables.extend(
            (
                _render_terminal_table(
                    (
                        "Metric",
                        version2_header,
                        version1_header,
                        "Delta (V2 - V1)",
                        "Change",
                        "Result",
                        "Reason",
                    ),
                    table_rows,
                ),
                "",
            )
        )

    summary = _normalized_differential_summary(records)
    if not tables:
        return f"No comparable metrics.\n\n{summary}"
    tables.append(summary)
    return "\n".join(tables).rstrip()


def _normalized_table_title(identity: dict[str, Any]) -> str:
    """Show the logical test identity and its DCDC firmware mode."""

    role = str(identity.get("firmware_role") or "unknown").upper()
    title = (
        f"### {identity.get('feature', 'unknown feature')} | "
        f"{identity.get('config_path', 'unknown config')} | DCDC mode: {role}"
    )
    if identity.get("config_hash"):
        title += f" | Config hash: {identity['config_hash']}"
    return title


def _normalized_identity_setup(identity: dict[str, Any]) -> str:
    """Render board and test setup identity retained in the normalized record."""

    setup_fields = (
        ("Board", identity.get("board")),
        ("Device", identity.get("device")),
        (
            "Supply voltage",
            None
            if identity.get("supply_voltage_v") is None
            else f"{identity['supply_voltage_v']} V",
        ),
        ("Partition", identity.get("partition")),
        ("Run mode", identity.get("mode")),
    )
    details = [f"{label}: {value}" for label, value in setup_fields if value not in (None, "")]
    return "Setup: " + (" | ".join(details) if details else "N/A")


def _normalized_metric_observation(
    variant: dict[str, Any],
    variable: str,
    comparison: dict[str, Any],
    comparison_field: str,
    label: str,
) -> tuple[str, str | None]:
    """Return one side's display value and its availability reason."""

    observations = variant.get("metrics")
    if isinstance(observations, dict) and variable in observations:
        observation = observations[variable]
        if isinstance(observation, dict):
            value = observation.get("value")
            if (
                observation.get("available") is True
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                return _format_number(value), None
            reason = observation.get("availability_reason") or "metric_value_unavailable"
            return "N/A", f"{label}: {reason}"
        return "N/A", f"{label}: invalid_metric_observation"

    # Older normalized schemas kept side values only on comparable metrics.
    value = comparison.get(comparison_field)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _format_number(value), None
    return "N/A", None


def _normalized_sdk_column_label(
    record: dict[str, Any], variant_name: str, fallback: str
) -> str:
    variant = record.get(variant_name)
    if not isinstance(variant, dict):
        return fallback
    provenance = variant.get("provenance")
    if not isinstance(provenance, dict):
        return fallback
    sisdk = (
        provenance.get("dut_sdk_version")
        or provenance.get("sisdk_version")
        or provenance.get("sdk_version")
    )
    if not sisdk and isinstance(provenance.get("sisdk"), dict):
        sisdk = provenance["sisdk"].get("version")
    return f"SiSDK {sisdk}" if isinstance(sisdk, str) and sisdk else fallback


def _normalized_variant_run_line(record: dict[str, Any], variant_name: str) -> str:
    """Show each version's SDK provenance, execution status, Bench, and time."""

    variant = record.get(variant_name)
    if not isinstance(variant, dict):
        variant = {}
    provenance = variant.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}

    details = [_normalized_sdk_column_label(record, variant_name, "SiSDK N/A")]
    sdk_build = (
        provenance.get("dut_sdk_artifactory_build")
        or provenance.get("sisdk_artifactory_build")
        or provenance.get("build_number")
    )
    if sdk_build:
        details.append(f"Build {sdk_build}")
    details.append(f"status {variant.get('execution_status') or 'unknown'}")
    if variant.get("cms_bench_id"):
        details.append(f"CMS Bench {variant['cms_bench_id']}")
    if variant.get("measured_at"):
        details.append(f"measured {variant['measured_at']}")
    label = "V1" if variant_name == "version1" else "V2"
    return f"{label} run: " + " | ".join(details)


def _table_title(identity: dict[str, Any]) -> str:
    return "### {feature} — {config_path} — {firmware_role}".format(
        feature=identity.get("feature", "unknown feature"),
        config_path=identity.get("config_path", "unknown config"),
        firmware_role=identity.get("firmware_role", "unknown role"),
    )


def _format_metric_name(variable: str, *, include_unit: bool = False) -> str:
    """Make known metric names concise without changing report keys."""

    name = (
        variable.removesuffix("_current_ma")
        .removesuffix("_energy_nj")
        .replace("_", " ")
    )
    unit = _METRIC_UNITS.get(variable)
    return f"{name} [{unit}]" if include_unit and unit else name


def _units_for_rows(rows: list[dict[str, Any]]) -> set[str]:
    return {
        _METRIC_UNITS[variable]
        for metric in rows
        if (variable := str(metric.get("variable", ""))) in _METRIC_UNITS
    }


def _metric_header(rows: list[dict[str, Any]]) -> str:
    units = _units_for_rows(rows)
    if len(units) == 1:
        return f"Metric [{next(iter(units))}]"
    return "Metric"


def _render_terminal_table_with_sisdk(
    pair: dict[str, Any], headers: tuple[str, ...], rows: list[tuple[str, ...]]
) -> str:
    """Replace generic variant labels with their recorded SiSDK versions."""

    concrete_headers = list(headers)
    concrete_headers[1] = _sisdk_column_label(pair, "version2", concrete_headers[1])
    concrete_headers[2] = _sisdk_column_label(pair, "version1", concrete_headers[2])
    return _render_terminal_table(tuple(concrete_headers), rows)


def _sisdk_column_label(pair: dict[str, Any], variant: str, fallback: str) -> str:
    """Return the concrete SiSDK version recorded for one comparison input."""

    variant_input = pair.get(variant)
    if not isinstance(variant_input, dict):
        return fallback
    provenance = variant_input.get("provenance")
    if not isinstance(provenance, dict):
        return fallback
    sisdk = provenance.get("sisdk_version")
    if not sisdk and isinstance(provenance.get("sisdk"), dict):
        sisdk = provenance["sisdk"].get("version")
    return f"SiSDK {sisdk}" if isinstance(sisdk, str) and sisdk else fallback


def _format_number(value: Any, *, signed: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "N/A"
    number = float(value)
    if number == 0:
        return "0"
    rendered = f"{abs(number):.6f}".rstrip("0").rstrip(".")
    if signed:
        return f"{'+' if number > 0 else '-'}{rendered}"
    return f"{'-' if number < 0 else ''}{rendered}"


def _format_percent(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "N/A"
    number = float(value)
    return f"{number:+.2f}%" if number else "0.00%"


def _format_status(status: str) -> str:
    return {
        "PASS": "✓ PASS",
        "WARNING": "! WARN",
        "FAIL": "✗ FAIL",
        "UNAVAILABLE": "— N/A",
    }.get(status, status)


def _render_terminal_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    """Return a compact aligned box table suitable for PowerShell and CI logs."""

    widths = [max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)]
    border = lambda left, fill, join, right: left + join.join(fill * (width + 2) for width in widths) + right
    render_row = lambda values: "│" + "│".join(
        f" {value:<{width}} " for value, width in zip(values, widths)
    ) + "│"
    return "\n".join(
        (
            border("┌", "─", "┬", "┐"),
            render_row(headers),
            border("├", "─", "┼", "┤"),
            *(render_row(row) for row in rows),
            border("└", "─", "┴", "┘"),
        )
    )


def _percent_delta(version2_value: float, version1_value: float) -> float | None:
    if version1_value == 0:
        return None
    return 100 * (version2_value - version1_value) / version1_value


def _comparison_status(
    percent: float | None, thresholds: PercentageThresholdPolicy
) -> ComparisonStatus:
    if percent is None:
        return ComparisonStatus.UNAVAILABLE
    if percent >= thresholds.failure_percent:
        return ComparisonStatus.FAIL
    if percent >= thresholds.warning_percent:
        return ComparisonStatus.WARNING
    return ComparisonStatus.PASS


def _identity_to_dict(identity: PairingIdentity) -> dict[str, Any]:
    return {
        "feature": identity.feature,
        "config_path": identity.config_path,
        "config_hash": identity.config_hash,
        "partition": identity.partition,
        "mode": identity.mode,
        "board": identity.board,
        "device": identity.device,
        "firmware_role": identity.firmware_role,
        "supply_voltage_v": identity.supply_voltage_v,
    }


def _report_variant_input(result: VariantResult) -> dict[str, Any]:
    """Extract a present variant's timing, bench, and firmware report metadata."""

    test_run = result.payload.get("test_run")
    if not isinstance(test_run, dict):
        test_run = {}
    storage = result.payload.get("storage")
    if not isinstance(storage, dict):
        storage = {}
    return {
        "artifact_path": result.path.as_posix(),
        "execution_status": test_run.get("status"),
        "measured_at": test_run.get("measured_at"),
        "cms_bench_id": storage.get("cms_bench_id"),
        "provenance": result.payload.get("firmware", {}),
        "metrics": _report_variant_metrics(result),
    }


def _report_variant_metrics(result: VariantResult) -> dict[str, dict[str, Any]]:
    """Return each available metric value and an explicit reason when it is absent."""

    payload = result.payload
    result_metric = payload.get("result_metric") if isinstance(payload, dict) else None
    observations: dict[str, dict[str, Any]] = {}
    summary_metrics = {
        "sleep_current_ma": ("Interval", "i_mean_ma"),
        "event_energy_nj": ("Event", "e_mean_nj"),
        "interval_energy_nj": ("Interval", "e_mean_nj"),
    }

    for variable in _METRIC_FIELDS:
        value: float | None = None
        try:
            if not isinstance(payload, dict):
                reason = "invalid_result_payload"
            elif not isinstance(result_metric, dict):
                reason = "missing_result_metric"
            else:
                reason = _metric_omission_reason(payload, variable)
                if reason is None:
                    if variable in summary_metrics:
                        meas_type, field = summary_metrics[variable]
                        value = _summary_metric(payload, meas_type, field)
                    else:
                        value = _optional_current(result_metric, variable)
                    if value is None:
                        reason = "null_metric"
        except ResultMetricParseError as exc:
            reason = str(exc)

        if reason is None:
            observations[variable] = {
                "value": value,
                "available": True,
                "availability_reason": None,
            }
            continue

        if reason == "marked_unavailable" and isinstance(result_metric, dict):
            metric_status = result_metric.get("metric_status")
            if isinstance(metric_status, dict):
                status = metric_status.get(variable)
                if isinstance(status, str) and status:
                    reason = status
        observations[variable] = {
            "value": None,
            "available": False,
            "availability_reason": reason,
        }

    return observations


def _missing_report_variant() -> dict[str, Any]:
    """Return missing-result metadata and mark every metric unavailable."""

    return {
        "artifact_path": None,
        "execution_status": "missing_result",
        "measured_at": None,
        "cms_bench_id": None,
        "provenance": {},
        "metrics": {
            variable: {
                "value": None,
                "available": False,
                "availability_reason": "missing_result",
            }
            for variable in _METRIC_FIELDS
        },
    }


def _classify_differential_record(
    metrics: list[dict[str, Any]], diagnostics: list[ComparisonDiagnostic]
) -> tuple[str, str | None]:
    """Return a test-level status and reason from its metric results and diagnostics.

    A failing metric takes precedence; otherwise diagnostics or an empty metric set
    make the record INCOMPLETE. A record with comparable, non-failing metrics is PASS.
    """

    failing_metrics = [
        metric
        for metric in metrics
        if metric.get("status") == ComparisonStatus.FAIL.value
    ]
    if failing_metrics:
        reasons = []
        for metric in failing_metrics:
            percent = metric.get("percent")
            rendered_percent = (
                "unavailable" if percent is None else f"{percent:+.2f}%"
            )
            reasons.append(f"{metric['variable']} regression ({rendered_percent})")
        return "FAIL", "; ".join(reasons)

    if diagnostics:
        reason_messages = list(
            dict.fromkeys(diagnostic.message for diagnostic in diagnostics)
        )
        return "INCOMPLETE", "; ".join(reason_messages)
    if not metrics:
        return "INCOMPLETE", "No comparable metrics are available."
    return "PASS", None


def _extract_or_diagnose(
    result: VariantResult, diagnostics: list[ComparisonDiagnostic]
) -> MeasurementMetrics | None:
    try:
        return extract_measurement_metrics(result.payload)
    except ResultMetricParseError as exc:
        diagnostics.append(
            ComparisonDiagnostic(
                code="malformed_result_metric",
                message=str(exc),
                variant=result.variant,
                identity=result.identity,
            )
        )
        return None


def _metric_omission_reason(payload: dict[str, Any], variable: str) -> str | None:
    summary_metric_types = {
        "sleep_current_ma": ("Interval", "i_mean_ma"),
        "event_energy_nj": ("Event", "e_mean_nj"),
        "interval_energy_nj": ("Interval", "e_mean_nj"),
    }
    if variable in summary_metric_types:
        meas_type, field = summary_metric_types[variable]
        summary = _summary_entry(payload, meas_type)
        if summary is None:
            return f"missing_{meas_type.lower()}_summary"
        if field not in summary:
            return "missing_metric"
        return "null_metric" if summary[field] is None else None

    result_metric = payload.get("result_metric")
    if not isinstance(result_metric, dict):
        return "missing_result_metric"
    if variable not in result_metric:
        return "missing_metric"
    metric_status = result_metric.get("metric_status", {})
    if isinstance(metric_status, dict) and "unavailable" in str(metric_status.get(variable, "")).lower():
        return "marked_unavailable"
    if result_metric[variable] is None:
        return "null_metric"
    return None


def _omission_diagnostic(
    result: VariantResult, variable: str, reason: str
) -> ComparisonDiagnostic:
    return ComparisonDiagnostic(
        code=reason,
        message=f"{variable} is not available for comparison: {reason}",
        variant=result.variant,
        variable=variable,
        identity=result.identity,
    )


def _interval_sleep_current(payload: dict[str, Any]) -> float | None:
    return _summary_metric(payload, "Interval", "i_mean_ma")


def _summary_metric(payload: dict[str, Any], meas_type: str, field: str) -> float | None:
    summary = _summary_entry(payload, meas_type)
    return _optional_number(summary, field) if summary is not None else None


def _summary_entry(payload: dict[str, Any], meas_type: str) -> dict[str, Any] | None:
    summaries = payload.get("result_summary")
    if summaries is None:
        return None
    if not isinstance(summaries, list):
        raise ResultMetricParseError("measurement result result_summary must be a list")
    matches: list[dict[str, Any]] = []
    for index, summary in enumerate(summaries, start=1):
        if not isinstance(summary, dict):
            raise ResultMetricParseError(f"result_summary entry {index} must be an object")
        if summary.get("meas_type") == meas_type:
            matches.append(summary)
    if not matches:
        return None
    if len(matches) != 1:
        raise ResultMetricParseError(f"result_summary has multiple {meas_type} entries")
    return matches[0]


def _optional_current(payload: dict[str, Any], field: str) -> float | None:
    return _optional_number(payload, field)


def _optional_number(payload: dict[str, Any], field: str) -> float | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResultMetricParseError(f"metric {field!r} must be a number or null")
    return float(value)


def pair_downloaded_differential_results(
    version1_root: Path,
    version2_root: Path,
    paired_plan_path: Path,
) -> PairingResult:
    """Pair version1 and version2 results from retained differential artifacts.

    ``paired_plan_path`` is authoritative for the DUT firmware role.  Raw
    result filenames and their modification times are deliberately ignored.
    """

    roles = _load_firmware_roles(paired_plan_path)
    version1_results = _load_variant_results("version1", version1_root, roles)
    version2_results = _load_variant_results("version2", version2_root, roles)
    _raise_for_duplicate_identities(version1_results, "version1")
    _raise_for_duplicate_identities(version2_results, "version2")

    version1_by_identity = {result.identity: result for result in version1_results}
    version2_by_identity = {result.identity: result for result in version2_results}
    identities = sorted(set(version1_by_identity) | set(version2_by_identity), key=_identity_sort_key)

    pairs: list[ResultPair] = []
    unmatched_version1: list[VariantResult] = []
    unmatched_version2: list[VariantResult] = []
    for identity in identities:
        version1 = version1_by_identity.get(identity)
        version2 = version2_by_identity.get(identity)
        if version1 is not None and version2 is not None:
            pairs.append(ResultPair(identity=identity, version1=version1, version2=version2))
        elif version1 is not None:
            unmatched_version1.append(version1)
        elif version2 is not None:
            unmatched_version2.append(version2)

    return PairingResult(
        pairs=tuple(pairs),
        unmatched_version1=tuple(unmatched_version1),
        unmatched_version2=tuple(unmatched_version2),
    )


def _load_firmware_roles(
    plan_path: Path,
) -> dict[tuple[str, str, str, str, str], PlannedJobIdentity]:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DifferentialPairingError(f"cannot read paired plan: {plan_path}") from exc
    except json.JSONDecodeError as exc:
        raise DifferentialPairingError(f"paired plan is not valid JSON: {plan_path}") from exc

    jobs = plan.get("jobs") if isinstance(plan, dict) else None
    if not isinstance(jobs, list):
        raise DifferentialPairingError("paired plan must contain a jobs list")

    roles: dict[tuple[str, str, str, str, str], PlannedJobIdentity] = {}
    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise DifferentialPairingError(f"paired plan job {index} must be an object")
        key = _plan_result_key(job, source=f"paired plan job {index}")
        role = _required_text(job, "firmware_role", source=f"paired plan job {index}")
        if role not in {"dcdc", "no_dcdc"}:
            raise DifferentialPairingError(
                f"paired plan job {index} has unsupported firmware role: {role}"
            )
        dut_slcp_value = job.get("dut_slcp", "")
        if not isinstance(dut_slcp_value, str):
            raise DifferentialPairingError(
                f"paired plan job {index} has no string 'dut_slcp' field"
            )
        dut_slcp = dut_slcp_value.strip()
        if dut_slcp and _firmware_role_from_slcp(dut_slcp, source=f"paired plan job {index}") != role:
            raise DifferentialPairingError(
                f"paired plan job {index} firmware role does not match its DUT application"
            )
        if key in roles:
            raise DifferentialPairingError(
                f"paired plan has duplicate raw-result identity: {key}"
            )
        roles[key] = PlannedJobIdentity(
            partition=_text_or_empty(job, "partition", source=f"paired plan job {index}"),
            firmware_role=role,
        )
    return roles


def _load_variant_results(
    variant: str,
    root: Path,
    roles: dict[tuple[str, str, str, str, str], PlannedJobIdentity],
) -> list[VariantResult]:
    if not root.is_dir():
        raise DifferentialPairingError(f"{variant} result root does not exist: {root}")

    results: list[VariantResult] = []
    for path in sorted(root.rglob("*.json")):
        payload = _load_result_payload(path)
        if payload is None:
            continue
        result_key = _result_key(payload, source=str(path))
        firmware = _required_object(payload, "firmware", source=str(path))
        dut_slcp = (
            _text_or_empty(firmware, "dut_slcp", source=str(path))
            if "dut_slcp" in firmware
            else ""
        )
        if dut_slcp:
            role = _firmware_role_from_slcp(dut_slcp, source=str(path))
            declared_role = firmware.get("firmware_role")
            if declared_role is not None and declared_role != role:
                raise DifferentialPairingError(
                    f"{path} firmware role does not match its DUT application"
                )
        else:
            declared_role = firmware.get("firmware_role")
            if declared_role is not None:
                if declared_role not in {"dcdc", "no_dcdc"}:
                    raise DifferentialPairingError(
                        f"{path} has an unsupported firmware role: {declared_role}"
                    )
                role = declared_role
            else:
                candidates = [key for key in roles if key[:4] == result_key]
                if len(candidates) != 1:
                    raise DifferentialPairingError(
                        f"{path} is missing DUT application identity for a multi-role paired plan"
                    )
                role = candidates[0][4]
        planned_identity = roles.get((*result_key, role))
        if planned_identity is None:
            raise DifferentialPairingError(
                f"result does not match a paired-plan job: {path}"
            )
        dataset = _required_object(payload, "dataset", source=str(path))
        raw_partition = dataset.get("partition")
        if raw_partition is not None:
            observed_partition = _text_or_empty(dataset, "partition", source=str(path))
            if observed_partition != planned_identity.partition:
                raise DifferentialPairingError(
                    f"{path} partition does not match its paired-plan job"
                )
        parameters = payload.get("parameters")
        if parameters is not None and not isinstance(parameters, dict):
            raise DifferentialPairingError(
                f"{path} has no object field 'parameters'"
            )
        supply_voltage = _optional_supply_voltage(
            parameters or {}, source=str(path)
        )
        results.append(
            VariantResult(
                variant=variant,
                path=path,
                identity=PairingIdentity(
                    feature=result_key[0],
                    config_path=result_key[1],
                    config_hash=result_key[2],
                    partition=planned_identity.partition,
                    mode=result_key[3],
                    board=_required_text(firmware, "board", source=str(path)),
                    device=_required_text(firmware, "device", source=str(path)),
                    firmware_role=planned_identity.firmware_role,
                    supply_voltage_v=supply_voltage,
                ),
                payload=payload,
            )
        )
    return results


def _load_result_payload(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DifferentialPairingError(f"result artifact is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise DifferentialPairingError(f"result artifact must be a JSON object: {path}")
    # Variant metadata and diagnostic JSON files share the retained roots.
    # Only raw measurement results participate in pairing.
    if not {"test_run", "dataset", "firmware"} <= set(payload):
        return None
    return payload


def _result_key(payload: dict[str, Any], *, source: str) -> tuple[str, str, str, str]:
    test_run = _required_object(payload, "test_run", source=source)
    dataset = _required_object(payload, "dataset", source=source)
    return (
        _required_text(test_run, "test_name", source=source),
        _normalised_path(_required_text(test_run, "config_path", source=source)),
        _required_text(test_run, "config_hash", source=source),
        _required_text(dataset, "mode", source=source),
    )


def _plan_result_key(
    job: dict[str, Any], *, source: str
) -> tuple[str, str, str, str, str]:
    return (
        _required_text(job, "feature", source=source),
        _normalised_path(_required_text(job, "config_path", source=source)),
        _required_text(job, "config_hash", source=source),
        _required_text(job, "mode", source=source),
        _required_text(job, "firmware_role", source=source),
    )


def _firmware_role_from_slcp(value: str, *, source: str) -> str:
    filename = value.replace("\\", "/").rsplit("/", 1)[-1]
    project = filename.rsplit(".", 1)[0].lower()
    if project == "ncp_ble_dut_dcdc":
        return "dcdc"
    if project == "ncp_ble_dut":
        return "no_dcdc"
    raise DifferentialPairingError(
        f"{source} uses unsupported DUT application: {value}"
    )


def _required_object(payload: dict[str, Any], field: str, *, source: str) -> dict[str, Any]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise DifferentialPairingError(f"{source} has no object field {field!r}")
    return value


def _required_text(payload: dict[str, Any], field: str, *, source: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DifferentialPairingError(f"{source} has no non-empty {field!r} field")
    return value.strip()


def _text_or_empty(payload: dict[str, Any], field: str, *, source: str) -> str:
    """Return an identity field whose empty string is semantically valid."""

    value = payload.get(field)
    if not isinstance(value, str):
        raise DifferentialPairingError(f"{source} has no string {field!r} field")
    return value.strip()


def _optional_supply_voltage(
    parameters: dict[str, Any], *, source: str
) -> float | None:
    value = parameters.get("supply_voltage_v")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DifferentialPairingError(
            f"{source} has no numeric 'parameters.supply_voltage_v' field"
        )
    voltage = float(value)
    if not math.isfinite(voltage):
        raise DifferentialPairingError(
            f"{source} has a non-finite 'parameters.supply_voltage_v' field"
        )
    return voltage


def _normalised_path(value: str) -> str:
    normalised = value.replace("\\", "/")
    marker = "tests/configs/"
    marker_index = normalised.lower().find(marker)
    return normalised[marker_index:] if marker_index >= 0 else normalised


def _raise_for_duplicate_identities(results: list[VariantResult], variant: str) -> None:
    seen: set[PairingIdentity] = set()
    for result in results:
        if result.identity in seen:
            raise DifferentialPairingError(
                f"{variant} has duplicate result identity: {result.identity}"
            )
        seen.add(result.identity)


def _identity_sort_key(identity: PairingIdentity) -> tuple[str, ...]:
    return (
        identity.feature,
        identity.config_path,
        identity.config_hash,
        identity.partition,
        identity.mode,
        identity.board,
        identity.device,
        identity.firmware_role,
        (
            ""
            if identity.supply_voltage_v is None
            else format(identity.supply_voltage_v, ".17g")
        ),
    )

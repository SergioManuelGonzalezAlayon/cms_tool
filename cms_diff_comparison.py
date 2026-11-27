"""Stable version1/version2 result pairing for CMS differential campaigns.

This module intentionally performs no network or CLI work.  It consumes the
already downloaded ``results/version1`` and ``results/version2`` artifact roots,
then uses the retained paired job plan to supply the explicit partition and
DUT firmware role for every raw measurement result.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import json
import math
from pathlib import Path
import statistics
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


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
    sample_count: int | None = None
    sample_statistics: tuple[tuple[str, SampleStatistics], ...] = ()
    sample_metric_issues: tuple[tuple[str, str], ...] = ()


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
class SampleStatistics:
    """Descriptive statistics for one metric across repeated measurements."""

    count: int
    mean: float
    median: float
    standard_deviation: float | None
    minimum: float
    maximum: float


@dataclass(frozen=True, slots=True)
class ComparisonMetricInput:
    """One metric with valid values available from both firmware variants."""

    identity: PairingIdentity
    variable: str
    version1_value: float
    version2_value: float
    version1_statistics: SampleStatistics | None = None
    version2_statistics: SampleStatistics | None = None


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

_SAMPLED_SUMMARY_METRICS = {
    "sleep_current_ma": ("Interval", "i_mean_ma"),
    "event_energy_nj": ("Event", "e_mean_nj"),
    "interval_energy_nj": ("Interval", "e_mean_nj"),
}

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


def summarize_sample_values(values: Iterable[int | float]) -> SampleStatistics:
    """Summarize finite numeric samples using sample standard deviation.

    Standard deviation is ``None`` for a single value because sample
    dispersion cannot be estimated from one measurement.
    """

    samples = tuple(values)
    if not samples:
        raise ResultMetricParseError("sample metric must contain at least one value")

    numeric_values: list[float] = []
    for index, value in enumerate(samples, start=1):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ResultMetricParseError(f"sample metric value {index} must be numeric")
        try:
            numeric_value = float(value)
        except (OverflowError, ValueError) as exc:
            raise ResultMetricParseError(
                f"sample metric value {index} must be finite"
            ) from exc
        if not math.isfinite(numeric_value):
            raise ResultMetricParseError(f"sample metric value {index} must be finite")
        numeric_values.append(numeric_value)

    return SampleStatistics(
        count=len(numeric_values),
        mean=statistics.fmean(numeric_values),
        median=statistics.median(numeric_values),
        standard_deviation=(
            statistics.stdev(numeric_values) if len(numeric_values) > 1 else None
        ),
        minimum=min(numeric_values),
        maximum=max(numeric_values),
    )


def _sample_statistics_to_dict(summary: SampleStatistics | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "count": summary.count,
        "mean": summary.mean,
        "median": summary.median,
        "standard_deviation": summary.standard_deviation,
        "minimum": summary.minimum,
        "maximum": summary.maximum,
    }


def _aggregate_sample_results(
    results: list[VariantResult], sample_count: int
) -> list[VariantResult]:
    """Aggregate all differential metrics for each validated sample identity."""

    grouped: dict[PairingIdentity, list[VariantResult]] = {}
    for result in results:
        grouped.setdefault(result.identity, []).append(result)

    aggregated: list[VariantResult] = []
    for identity, samples in grouped.items():
        if len(samples) != sample_count:
            raise DifferentialPairingError(
                f"{samples[0].variant} sample set has {len(samples)} results for "
                f"identity {identity}; expected {sample_count}"
            )
        ordered_samples = sorted(samples, key=_sample_result_index)
        statistics_by_metric: dict[str, SampleStatistics] = {}
        issues_by_metric: dict[str, str] = {}
        for variable in _METRIC_FIELDS:
            values: list[float] = []
            issues: list[str] = []
            for result in ordered_samples:
                try:
                    omission_reason = _metric_omission_reason(result.payload, variable)
                    if omission_reason is not None:
                        issues.append(omission_reason)
                        continue

                    if variable in _SAMPLED_SUMMARY_METRICS:
                        value = _summary_metric(
                            result.payload, *_SAMPLED_SUMMARY_METRICS[variable]
                        )
                    else:
                        result_metric = result.payload.get("result_metric")
                        if not isinstance(result_metric, dict):
                            issues.append("missing_result_metric")
                            continue
                        value = _optional_number(result_metric, variable)
                    if value is None:
                        issues.append("null_metric")
                    else:
                        values.append(value)
                except ResultMetricParseError as exc:
                    issues.append(str(exc))

            if len(values) == sample_count:
                try:
                    statistics_by_metric[variable] = summarize_sample_values(values)
                    continue
                except ResultMetricParseError as exc:
                    issues.append(str(exc))

            if len(values) == 0 and issues and len(set(issues)) == 1:
                issues_by_metric[variable] = issues[0]
            else:
                issues_by_metric[variable] = "incomplete_sample_metric_set"

        ordered_first = ordered_samples[0]
        aggregated.append(
            replace(
                ordered_first,
                sample_count=sample_count,
                sample_statistics=tuple(statistics_by_metric.items()),
                sample_metric_issues=tuple(issues_by_metric.items()),
            )
        )
    return aggregated


def _sample_result_index(result: VariantResult) -> int:
    test_run = result.payload.get("test_run")
    if not isinstance(test_run, dict):
        raise DifferentialPairingError(f"{result.path} has no test_run object")
    sample_index = test_run.get("sample_index")
    if type(sample_index) is not int or sample_index < 1:
        raise DifferentialPairingError(f"{result.path} has an invalid sample_index")
    return sample_index


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
        if pair.version1.sample_count is not None or pair.version2.sample_count is not None:
            version1_statistics = dict(pair.version1.sample_statistics)
            version2_statistics = dict(pair.version2.sample_statistics)
            version1_issues = dict(pair.version1.sample_metric_issues)
            version2_issues = dict(pair.version2.sample_metric_issues)
            for variable in _METRIC_FIELDS:
                first = version1_statistics.get(variable)
                second = version2_statistics.get(variable)
                first_issue = version1_issues.get(variable)
                second_issue = version2_issues.get(variable)
                if (
                    first is None
                    and second is None
                    and first_issue == second_issue == "marked_unavailable"
                ):
                    continue
                if (
                    variable in {"event_energy_nj", "interval_energy_nj"}
                    and first is None
                    and second is None
                    and first_issue == second_issue
                    and first_issue is not None
                    and first_issue.startswith("missing_")
                ):
                    continue
                if first is None:
                    diagnostics.append(
                        _omission_diagnostic(
                            pair.version1,
                            variable,
                            first_issue or "missing_sample_statistics",
                        )
                    )
                if second is None:
                    diagnostics.append(
                        _omission_diagnostic(
                            pair.version2,
                            variable,
                            second_issue or "missing_sample_statistics",
                        )
                    )
                if first is None or second is None:
                    continue
                metrics.append(
                    ComparisonMetricInput(
                        identity=pair.identity,
                        variable=variable,
                        version1_value=first.mean,
                        version2_value=second.mean,
                        version1_statistics=first,
                        version2_statistics=second,
                    )
                )
            continue

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
                    "reason": _comparison_reason(percent, status),
                    "thresholds": {
                        "basis": "percent_regression",
                        "warning_percent": thresholds.warning_percent,
                        "failure_percent": thresholds.failure_percent,
                    },
                    **(
                        {
                            "version1_statistics": _sample_statistics_to_dict(
                                metric.version1_statistics
                            ),
                            "version2_statistics": _sample_statistics_to_dict(
                                metric.version2_statistics
                            ),
                        }
                        if metric.version1_statistics is not None
                        and metric.version2_statistics is not None
                        else {}
                    ),
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
    """Write grouped results and the corresponding SiSDK metadata sheets.

    Each normalized test identity contributes one row per terminal metric. Test
    and firmware-role labels are merged across their metric rows; execution
    evidence is retained in trailing, hidden columns for audit purposes.
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
    test_group_fields = tuple(
        field for field in identity_fields if field != "firmware_role"
    )
    identity_keys: set[tuple[Any, ...]] = set()
    row_keys: set[tuple[tuple[Any, ...], str]] = set()
    mode_ranges: list[tuple[int, int]] = []
    expected_merged_ranges: list[str] = []
    test_group_rows: dict[tuple[Any, ...], tuple[int, int]] = {}
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
        "Configuration",
        "Measurement evidence",
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
    rules = workbook.create_sheet("Comparison rules")
    thresholds = report.get("thresholds") or {}
    rules.append(["Comparison", "Change (%) = 100 × (V2 − V1) / V1; V1 is the baseline."])
    rules.append(["Warning", f"Increase ≥ {thresholds.get('warning_percent', 1.5)}% and below the failure threshold."])
    rules.append(["Failure", f"Increase ≥ {thresholds.get('failure_percent', 2.0)}%, or absolute change ≥ 10%."])
    rules.append(["Pass", "All other available changes."])
    rules.append(["Unavailable", "A zero baseline or missing metric prevents comparison."])
    rules.append(["Evidence", "Result paths identify the measurement evidence for each side."])
    rules.column_dimensions["A"].width = 20
    rules.column_dimensions["B"].width = 90
    sdk_sheets: dict[str, dict[str, Any]] = {}

    valid_records: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("identity"), dict):
            raise ValueError("diff comparison report contains an invalid normalized record")
        identity = record["identity"]
        identity_key = tuple(identity.get(field) for field in identity_fields)
        if identity_key in identity_keys:
            raise ValueError("diff comparison report contains a duplicate test identity")
        identity_keys.add(identity_key)
        test_group_key = tuple(identity.get(field) for field in test_group_fields)
        valid_records.append((test_group_key, record))

    valid_records.sort(
        key=lambda item: (
            tuple(str(value or "") for value in item[0]),
            str(item[1]["identity"].get("firmware_role") or "").upper(),
        )
    )

    for test_group_key, record in valid_records:
        identity = record["identity"]
        identity_key = tuple(identity.get(field) for field in identity_fields)
        variants = {
            name: record.get(name) if isinstance(record.get(name), dict) else {}
            for name in ("version1", "version2")
        }
        config_path = str(identity.get("config_path") or "")
        config_name = Path(config_path).name if config_path else "N/A"
        test_name = str(identity.get("feature") or "Unknown test")
        if config_name and config_name != "N/A":
            test_name = f"{test_name}"
        role = str(identity.get("firmware_role") or "UNKNOWN").upper()
        run_details: dict[str, str] = {}
        run_sheets: dict[str, str] = {}
        for variant_name in ("version1", "version2"):
            variant = variants[variant_name]
            provenance = variant.get("provenance")
            if not isinstance(provenance, dict):
                provenance = {}
            metadata = provenance.get("sisdk_metadata")
            if isinstance(metadata, dict) and metadata.get("status") == "complete":
                version = str(metadata.get("version") or "")
                build = str(metadata.get("build_number") or "")
                libraries = metadata.get("libraries")
                if version and build.isdecimal() and isinstance(libraries, list):
                    sheet_name = f"SiSDK_{version}_{build}"
                    if len(sheet_name) > 31 or any(c in sheet_name for c in '[]:*?/\\'):
                        raise ValueError(f"invalid SiSDK metadata sheet name: {sheet_name}")
                    if sheet_name in sdk_sheets and (
                        sdk_sheets[sheet_name].get("libraries") != libraries
                        or sdk_sheets[sheet_name].get("conan_lock", {}).get("sha256")
                        != metadata.get("conan_lock", {}).get("sha256")
                    ):
                        raise ValueError(f"conflicting metadata for {sheet_name}")
                    if sheet_name not in sdk_sheets:
                        sdk_sheets[sheet_name] = metadata
                        sdk_sheet = workbook.create_sheet(sheet_name)
                        sdk_sheet.append(["SiSDK version", version])
                        sdk_sheet.append(["Build", build])
                        sdk_sheet.append(["Lockfile source", metadata.get("conan_lock", {}).get("source", "")])
                        sdk_sheet.append(["Lockfile SHA-256", metadata.get("conan_lock", {}).get("sha256", "")])
                        sdk_sheet.append(["Library", "Version and channel", "Revision", "Timestamp"])
                        for entry in libraries:
                            if not isinstance(entry, str):
                                raise ValueError(f"invalid lockfile entry for {sheet_name}")
                            reference, separator, revision_time = entry.partition("#")
                            library, _, package_version = reference.partition("/")
                            revision, _, timestamp = revision_time.partition("%")
                            sdk_sheet.append([library, package_version, revision if separator else "", timestamp])
                        sdk_sheet.freeze_panes = "A6"
                        for column, width in {"A": 36, "B": 40, "C": 46, "D": 22}.items():
                            sdk_sheet.column_dimensions[column].width = width
                    run_sheets[variant_name] = sheet_name
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
            if isinstance(metadata, dict):
                sdk_version = metadata.get("version") or sdk_version
                sdk_build = metadata.get("build_number") or sdk_build
                run_parts = [f"SiSDK {sdk_version}" if sdk_version else "SiSDK N/A"]
            if variant_name not in run_sheets:
                run_parts.append("metadata unavailable")
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
        sampled_record = any(
            variant.get("sample_count") is not None for variant in variants.values()
        )
        record_start_row = worksheet.max_row + 1
        metric_order = (
            tuple(_SAMPLED_SUMMARY_METRICS)
            if sampled_record
            else _TABLE_METRIC_ORDER
        )
        for variable in metric_order:
            row_key = (identity_key, variable)
            if row_key in row_keys:
                raise ValueError("diff comparison report contains a duplicate test metric")
            row_keys.add(row_key)

            observations: dict[str, dict[str, Any]] = {}
            reasons: list[str] = []
            record_reason = record.get("reason")
            comparison = comparisons.get(variable, {})
            metric_reason = comparison.get("reason")
            if metric_reason:
                reasons.append(str(metric_reason))
            elif record.get("differential_status") == "INCOMPLETE" and record_reason:
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
                "\n".join(
                    f"{label}: {json.dumps(variants[name].get('test_configuration'), sort_keys=True)}"
                    if isinstance(variants[name].get('test_configuration'), dict)
                    else f"{label}: configuration unavailable"
                    for name, label in (("version1", "V1"), ("version2", "V2"))
                ),
                "\n".join(f"{label}: {variants[name].get('artifact_path') or 'unavailable'}"
                          for name, label in (("version1", "V1"), ("version2", "V2"))),
            ]
            worksheet.append(row)
            for variant_name, column in (("version1", 11), ("version2", 12)):
                if variant_name in run_sheets:
                    cell = worksheet.cell(row=worksheet.max_row, column=column)
                    cell.hyperlink = f"#'{run_sheets[variant_name]}'!A1"
                    cell.font = Font(color="0563C1", underline="single")
            row_fill = PatternFill(
                fill_type="solid",
                fgColor="EAF1F8" if (worksheet.max_row % 2) == 0 else "FFFFFF",
            )
            for cell in worksheet[worksheet.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
                cell.fill = row_fill
                cell.border = Border(bottom=Side(style="thin", color="B4C7E7"))

        record_end_row = worksheet.max_row
        mode_ranges.append((record_start_row, record_end_row))
        previous_range = test_group_rows.get(test_group_key)
        if previous_range is None:
            test_group_rows[test_group_key] = (record_start_row, record_end_row)
        else:
            test_group_rows[test_group_key] = (previous_range[0], record_end_row)

    group_fill = PatternFill(fill_type="solid", fgColor="D9E5F2")
    for start_row, end_row in mode_ranges:
        for row_number in range(start_row, end_row + 1):
            worksheet.cell(row=row_number, column=2).fill = group_fill
        worksheet.cell(row=start_row, column=2).alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )
        worksheet.merge_cells(
            start_row=start_row,
            start_column=2,
            end_row=end_row,
            end_column=2,
        )
        expected_merged_ranges.append(f"B{start_row}:B{end_row}")

    for start_row, end_row in test_group_rows.values():
        for row_number in range(start_row, end_row + 1):
            worksheet.cell(row=row_number, column=1).fill = group_fill
        worksheet.cell(row=start_row, column=1).alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )
        worksheet.merge_cells(
            start_row=start_row,
            start_column=1,
            end_row=end_row,
            end_column=1,
        )
        expected_merged_ranges.append(f"A{start_row}:A{end_row}")

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
        "N": 70,
        "O": 70,
    }
    for column, width in column_widths.items():
        worksheet.column_dimensions[column].width = width
    worksheet.column_dimensions.group("I", "J", outline_level=1, hidden=True)
    for row in worksheet.iter_rows(min_row=2):
        worksheet.row_dimensions[row[0].row].height = 60
        for cell in row:
            cell.alignment = Alignment(vertical="center")
        for cell in row[13:15]:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
    for start_row, end_row in mode_ranges:
        worksheet.cell(row=start_row, column=2).alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )
    for start_row, end_row in test_group_rows.values():
        worksheet.cell(row=start_row, column=1).alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )
    for row in range(2, worksheet.max_row + 1):
        for column in (4, 5):
            worksheet.cell(row=row, column=column).number_format = "0.######;-0.######;0"
        worksheet.cell(row=row, column=6).number_format = "+0.######;-0.######;0"
        worksheet.cell(row=row, column=7).number_format = '+0.00"%";-0.00"%";0.00"%"'

    status_columns = (headers.index("Result") + 1, headers.index("Test Result") + 1)
    if worksheet.max_row > 1:
        for column in status_columns:
            _apply_status_conditional_formatting(worksheet, column)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    _validate_diff_excel_workbook(
        output_path,
        headers,
        len(row_keys),
        status_columns if worksheet.max_row > 1 else (),
        tuple(sorted(expected_merged_ranges)),
    )
    return output_path


def _apply_status_conditional_formatting(worksheet: Any, column: int) -> None:
    """Highlight one result column so metric and test outcomes stand out."""

    if worksheet.max_row < 2:
        return
    column_letter = get_column_letter(column)
    cell_range = f"{column_letter}2:{column_letter}{worksheet.max_row}"
    status_colors = {
        "PASS": ("D9EAD3", "008000"),
        "WARNING": ("FFF2CC", "C65911"),
        "FAIL": ("F4CCCC", "FF0000"),
        "INCOMPLETE": ("FCE5CD", "C65911"),
        "UNAVAILABLE": ("FCE4D6", "C65911"),
    }
    for status, (fill_color, font_color) in status_colors.items():
        worksheet.conditional_formatting.add(
            cell_range,
            FormulaRule(
                formula=[f'{column_letter}2="{status}"'],
                stopIfTrue=True,
                fill=PatternFill(fill_type="solid", fgColor=fill_color),
                font=Font(color=font_color),
            ),
        )


def _validate_diff_excel_workbook(
    output_path: Path,
    expected_headers: tuple[str, ...],
    expected_row_count: int,
    expected_status_columns: tuple[int, ...],
    expected_merged_ranges: tuple[str, ...],
) -> None:
    """Check the saved workbook's grouped layout, row count, and highlights."""

    workbook = load_workbook(output_path, data_only=True)
    try:
        if not workbook.sheetnames or workbook.sheetnames[0] != "Differential Results":
            raise ValueError("saved diff comparison workbook is missing results worksheet")
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
        if worksheet.tables:
            raise ValueError("saved diff comparison workbook must use a grouped worksheet range")
        if worksheet.freeze_panes != "D2":
            raise ValueError("saved diff comparison workbook is missing frozen headers and identifiers")
        actual_merged_ranges = tuple(sorted(str(rng) for rng in worksheet.merged_cells.ranges))
        if actual_merged_ranges != expected_merged_ranges:
            raise ValueError("saved diff comparison workbook has unexpected test and mode groups")
        if any(
            worksheet.cell(row=row, column=3).value is None
            for row in range(2, worksheet.max_row + 1)
        ):
            raise ValueError("saved diff comparison workbook contains a row without a metric")
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
        sampled = any(variant.get("sample_count") is not None for variant in variants.values())
        table_rows: list[tuple[str, ...]] = []
        metric_order = (
            tuple(_SAMPLED_SUMMARY_METRICS)
            if sampled
            else _TABLE_METRIC_ORDER
        )
        for variable in metric_order:
            comparison = comparisons.get(variable, {})
            version2_value, version2_reason = _normalized_metric_observation(
                variants["version2"], variable, comparison, "version2", "V2"
            )
            version1_value, version1_reason = _normalized_metric_observation(
                variants["version1"], variable, comparison, "version1", "V1"
            )
            reasons = ([str(comparison["reason"])] if comparison.get("reason") else []) + [
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
        headers = (
            "Metric",
            version2_header,
            version1_header,
            "Delta (V2 - V1)",
            "Change",
            "Result",
            "Reason",
        )
        tables.extend(
            (
                _render_terminal_table(
                    headers,
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
    if abs(percent) >= 10:
        return ComparisonStatus.FAIL
    if percent >= thresholds.failure_percent:
        return ComparisonStatus.FAIL
    if percent >= thresholds.warning_percent:
        return ComparisonStatus.WARNING
    return ComparisonStatus.PASS


def _comparison_reason(percent: float | None, status: ComparisonStatus) -> str | None:
    if percent is None or status is ComparisonStatus.UNAVAILABLE:
        return None
    if abs(percent) >= 10:
        return "Requires investigation. Power consumption range too large."
    if status is ComparisonStatus.PASS:
        return "Performance increased" if percent < 0 else "Within acceptable range"
    if percent >= 5:
        return "Power consumption critically increased"
    return "Power consumption increased"


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
        "test_configuration": result.payload.get("test_configuration"),
        **(
            {"sample_count": result.sample_count}
            if result.sample_count is not None
            else {}
        ),
        "metrics": _report_variant_metrics(result),
    }


def _report_variant_metrics(result: VariantResult) -> dict[str, dict[str, Any]]:
    """Return each available metric value and an explicit reason when it is absent."""

    payload = result.payload
    if result.sample_count is not None:
        statistics_by_metric = dict(result.sample_statistics)
        issues_by_metric = dict(result.sample_metric_issues)
        observations: dict[str, dict[str, Any]] = {}
        for variable in _METRIC_FIELDS:
            summary = statistics_by_metric.get(variable)
            if summary is not None:
                observations[variable] = {
                    "value": summary.mean,
                    "available": True,
                    "availability_reason": None,
                }
            else:
                availability_reason = issues_by_metric.get(
                    variable, "missing_sample_statistics"
                )
                if availability_reason == "marked_unavailable":
                    availability_reason = _metric_status_reason(
                        result.payload, variable
                    ) or availability_reason
                observations[variable] = {
                    "value": None,
                    "available": False,
                    "availability_reason": availability_reason,
                }
        return observations

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

        if reason == "marked_unavailable":
            reason = _metric_status_reason(payload, variable) or reason
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
        reasons = list(dict.fromkeys(
            str(metric["reason"])
            for metric in failing_metrics
            if metric.get("reason")
        ))
        return "FAIL", "; ".join(reasons) or "Power consumption increased"

    if diagnostics:
        reason_messages = list(
            dict.fromkeys(diagnostic.message for diagnostic in diagnostics)
        )
        return "INCOMPLETE", "; ".join(reason_messages)
    if not metrics:
        return "INCOMPLETE", "No comparable metrics are available."
    reasons = list(dict.fromkeys(
        str(metric["reason"])
        for metric in metrics
        if metric.get("reason")
    ))
    return "PASS", "; ".join(reasons) if reasons else None


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


def _metric_status_reason(payload: dict[str, Any], variable: str) -> str | None:
    result_metric = payload.get("result_metric")
    if not isinstance(result_metric, dict):
        return None
    metric_status = result_metric.get("metric_status")
    if not isinstance(metric_status, dict):
        return None
    status = metric_status.get(variable)
    return status if isinstance(status, str) and status else None


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

    plan = _load_paired_plan(paired_plan_path)
    _validate_differential_sample_sets(plan, version1_root, version2_root)
    roles = _load_firmware_roles(paired_plan_path, plan=plan)
    sample_count = plan["samples"] if "samples" in plan else None
    version1_results = _load_variant_results(
        "version1", version1_root, roles, sample_count=sample_count
    )
    version2_results = _load_variant_results(
        "version2", version2_root, roles, sample_count=sample_count
    )
    if "samples" in plan:
        version1_results = _aggregate_sample_results(version1_results, sample_count)
        version2_results = _aggregate_sample_results(version2_results, sample_count)
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


def _load_paired_plan(plan_path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DifferentialPairingError(f"cannot read paired plan: {plan_path}") from exc
    except json.JSONDecodeError as exc:
        raise DifferentialPairingError(f"paired plan is not valid JSON: {plan_path}") from exc
    if not isinstance(plan, dict):
        raise DifferentialPairingError("paired plan must be a JSON object")
    return plan


def _validate_differential_sample_sets(
    plan: dict[str, Any],
    version1_root: Path,
    version2_root: Path,
) -> None:
    """Validate repeated differential samples before the single-result pairing path."""

    if "samples" not in plan:
        return
    sample_count = plan.get("samples")
    if type(sample_count) is not int or sample_count < 1:
        raise DifferentialPairingError("paired plan samples must be a positive integer")
    selector = plan.get("selector")
    if not isinstance(selector, dict) or selector.get("config") != "default":
        raise DifferentialPairingError("sampled differential plans must select the default config")
    variable_filters = plan.get("variable_filters", [])
    if variable_filters != []:
        raise DifferentialPairingError("sampled differential plans cannot use variable filters")

    jobs = plan.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise DifferentialPairingError("sampled paired plan must contain jobs")

    expected: dict[tuple[str, str, str, str, str], dict[int, tuple[str, str, str, float | None]]] = {}
    metadata_by_identity: dict[tuple[str, str, str, str, str], tuple[str, str, str, float | None]] = {}
    role_sets: dict[tuple[str, str, str, str], set[str]] = {}
    for index, job in enumerate(jobs, start=1):
        source = f"paired plan job {index}"
        if not isinstance(job, dict):
            raise DifferentialPairingError(f"{source} must be an object")
        identity = _plan_result_key(job, source=source)
        role = identity[4]
        if role not in {"dcdc", "no_dcdc"}:
            raise DifferentialPairingError(f"{source} has unsupported firmware role: {role}")
        dut_slcp = _required_text(job, "dut_slcp", source=source)
        if _firmware_role_from_slcp(dut_slcp, source=source) != role:
            raise DifferentialPairingError(f"{source} firmware role does not match its DUT application")
        sample_index = job.get("sample_index")
        if type(sample_index) is not int or not 1 <= sample_index <= sample_count:
            raise DifferentialPairingError(
                f"{source} sample_index must be between 1 and {sample_count}"
            )
        supply_value = job.get("supply_v")
        if supply_value in (None, ""):
            planned_supply = None
        elif isinstance(supply_value, bool):
            raise DifferentialPairingError(f"{source} supply_v must be numeric")
        else:
            try:
                planned_supply = float(supply_value)
            except (TypeError, ValueError) as exc:
                raise DifferentialPairingError(f"{source} supply_v must be numeric") from exc
            if not math.isfinite(planned_supply):
                raise DifferentialPairingError(f"{source} supply_v must be finite")
        metadata = (
            _text_or_empty(job, "partition", source=source),
            _text_or_empty(job, "board", source=source),
            dut_slcp,
            planned_supply,
        )
        previous_metadata = metadata_by_identity.setdefault(identity, metadata)
        if previous_metadata != metadata:
            raise DifferentialPairingError(
                f"sample jobs disagree on configuration or firmware metadata: {identity}"
            )
        samples_for_identity = expected.setdefault(identity, {})
        if sample_index in samples_for_identity:
            raise DifferentialPairingError(
                f"paired plan repeats sample {sample_index} for identity: {identity}"
            )
        samples_for_identity[sample_index] = metadata
        role_sets.setdefault(identity[:4], set()).add(role)

    wanted_samples = set(range(1, sample_count + 1))
    for identity, samples_for_identity in expected.items():
        observed_samples = set(samples_for_identity)
        if observed_samples != wanted_samples:
            missing = sorted(wanted_samples - observed_samples)
            raise DifferentialPairingError(
                f"paired plan sample set is incomplete for {identity}; missing samples {missing}"
            )
    for config_identity, roles in role_sets.items():
        if roles != {"dcdc", "no_dcdc"}:
            raise DifferentialPairingError(
                f"sampled paired plan must include DCDC and NO_DCDC jobs for {config_identity}"
            )

    observed: dict[str, dict[tuple[str, str, str, str, str], set[int]]] = {
        "version1": {},
        "version2": {},
    }
    sdk_versions: dict[str, set[str]] = {"version1": set(), "version2": set()}
    environments: dict[
        tuple[str, str, str, str, str], tuple[str, str, float | None]
    ] = {}
    for variant, root in (("version1", version1_root), ("version2", version2_root)):
        if not root.is_dir():
            raise DifferentialPairingError(f"{variant} result root does not exist: {root}")
        seen = observed[variant]
        for path in sorted(root.rglob("*.json")):
            payload = _load_result_payload(path)
            if payload is None:
                continue
            result_key = _result_key(payload, source=str(path))
            test_run = _required_object(payload, "test_run", source=str(path))
            dataset = _required_object(payload, "dataset", source=str(path))
            firmware = _required_object(payload, "firmware", source=str(path))
            dut_slcp = _required_text(firmware, "dut_slcp", source=str(path))
            role = _firmware_role_from_slcp(dut_slcp, source=str(path))
            declared_role = firmware.get("firmware_role")
            if declared_role is not None and declared_role != role:
                raise DifferentialPairingError(f"{path} firmware role does not match its DUT application")
            identity = (*result_key, role)
            planned_metadata = metadata_by_identity.get(identity)
            if planned_metadata is None:
                raise DifferentialPairingError(f"sample result does not match a paired-plan job: {path}")
            sample_index = test_run.get("sample_index")
            if type(sample_index) is not int or sample_index not in expected[identity]:
                raise DifferentialPairingError(f"{path} has an unexpected or missing sample_index")
            samples_for_identity = seen.setdefault(identity, set())
            if sample_index in samples_for_identity:
                raise DifferentialPairingError(
                    f"{variant} has duplicate sample {sample_index} for identity: {identity}"
                )
            samples_for_identity.add(sample_index)

            partition, planned_board, _, planned_supply = planned_metadata
            raw_partition = dataset.get("partition")
            if raw_partition is not None:
                observed_partition = _text_or_empty(dataset, "partition", source=str(path))
                if observed_partition != partition:
                    raise DifferentialPairingError(f"{path} partition does not match its paired-plan job")
            board = _required_text(firmware, "board", source=str(path))
            device = _required_text(firmware, "device", source=str(path))
            if planned_board and board != planned_board:
                raise DifferentialPairingError(f"{path} board does not match its paired-plan job")
            parameters = payload.get("parameters")
            if not isinstance(parameters, dict):
                raise DifferentialPairingError(f"{path} has no object field 'parameters'")
            supply = _optional_supply_voltage(parameters, source=str(path))
            if planned_supply is not None and (
                supply is None or not math.isclose(supply, planned_supply, rel_tol=0.0, abs_tol=1e-9)
            ):
                raise DifferentialPairingError(f"{path} supply voltage does not match its paired-plan job")
            environment = (board, device, supply)
            previous_environment = environments.setdefault(identity, environment)
            if previous_environment != environment:
                raise DifferentialPairingError(
                    f"sample results disagree on board, device, or supply voltage for {identity}"
                )

            result_variant = _required_text(firmware, "variant", source=str(path))
            if result_variant != variant:
                raise DifferentialPairingError(
                    f"{path} firmware variant is {result_variant!r}; expected {variant!r}"
                )
            firmware_sdk = _required_text(firmware, "dut_sdk_version", source=str(path))
            dataset_sdk = _required_text(dataset, "sdk_version", source=str(path))
            if firmware_sdk != dataset_sdk:
                raise DifferentialPairingError(f"{path} SDK version fields disagree")
            sdk_versions[variant].add(firmware_sdk)

        for identity, sample_jobs in expected.items():
            actual_samples = seen.get(identity, set())
            expected_samples = set(sample_jobs)
            if actual_samples != expected_samples:
                missing = sorted(expected_samples - actual_samples)
                raise DifferentialPairingError(
                    f"{variant} sample set is incomplete for {identity}; missing samples {missing}"
                )
        if len(sdk_versions[variant]) != 1:
            raise DifferentialPairingError(
                f"{variant} samples do not resolve to one consistent SDK version"
            )


def _load_firmware_roles(
    plan_path: Path,
    *,
    plan: dict[str, Any] | None = None,
) -> dict[tuple[str, ...], PlannedJobIdentity]:
    plan = plan if plan is not None else _load_paired_plan(plan_path)

    jobs = plan.get("jobs")
    if not isinstance(jobs, list):
        raise DifferentialPairingError("paired plan must contain a jobs list")

    roles: dict[tuple[str, ...], PlannedJobIdentity] = {}
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
        if "samples" in plan:
            sample_index = job.get("sample_index")
            if type(sample_index) is not int or sample_index < 1:
                raise DifferentialPairingError(
                    f"paired plan job {index} has an invalid sample_index"
                )
            lookup_key = (*key, sample_index)
        else:
            lookup_key = key
        if lookup_key in roles:
            raise DifferentialPairingError(
                f"paired plan has duplicate raw-result identity: {lookup_key}"
            )
        roles[lookup_key] = PlannedJobIdentity(
            partition=_text_or_empty(job, "partition", source=f"paired plan job {index}"),
            firmware_role=role,
        )
    return roles


def _load_variant_results(
    variant: str,
    root: Path,
    roles: dict[tuple[str, ...], PlannedJobIdentity],
    *,
    sample_count: int | None = None,
) -> list[VariantResult]:
    if not root.is_dir():
        raise DifferentialPairingError(f"{variant} result root does not exist: {root}")

    results: list[VariantResult] = []
    for path in sorted(root.rglob("*.json")):
        payload = _load_result_payload(path)
        if payload is None:
            continue
        result_key = _result_key(payload, source=str(path))
        test_run = _required_object(payload, "test_run", source=str(path))
        sample_index = test_run.get("sample_index") if sample_count is not None else None
        if sample_count is not None and (
            type(sample_index) is not int or not 1 <= sample_index <= sample_count
        ):
            raise DifferentialPairingError(f"{path} has an unexpected or missing sample_index")
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
                candidates = [
                    key
                    for key in roles
                    if key[:4] == result_key
                    and (sample_count is None or key[-1] == sample_index)
                ]
                candidate_roles = {key[4] for key in candidates}
                if len(candidate_roles) != 1:
                    raise DifferentialPairingError(
                        f"{path} is missing DUT application identity for a multi-role paired plan"
                    )
                role = next(iter(candidate_roles))
        planned_key = (
            (*result_key, role, sample_index)
            if sample_count is not None
            else (*result_key, role)
        )
        planned_identity = roles.get(planned_key)
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

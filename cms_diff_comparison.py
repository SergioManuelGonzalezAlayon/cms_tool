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
from pathlib import Path
from typing import Any


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
    """Current values extracted from one raw measurement result artifact."""

    avg_current_ma: float | None
    peak_current_ma: float | None
    rx_current_ma: float | None
    tx_current_ma: float | None
    sleep_current_ma: float | None


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
            }
        return payload


@dataclass(frozen=True, slots=True)
class ComparisonInputs:
    """Comparable values plus retained diagnostics for later JSON reporting."""

    metrics: tuple[ComparisonMetricInput, ...]
    diagnostics: tuple[ComparisonDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class PercentageThresholdPolicy:
    """Initial differential status policy for positive current regressions."""

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
)

_TABLE_METRIC_ORDER = (
    "avg_current_ma",
    "peak_current_ma",
    "rx_current_ma",
    "tx_current_ma",
    "sleep_current_ma",
)


def extract_measurement_metrics(payload: dict[str, Any]) -> MeasurementMetrics:
    """Extract Phase 4 metrics from the established raw-result schema.

    ``result_metric`` contains average, peak, RX, and TX values.  Sleep
    current is specifically the ``i_mean_ma`` value of the sole
    ``result_summary`` entry whose ``meas_type`` is ``Interval``.
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
    """Build the versioned authoritative report for a differential campaign."""

    metrics_by_identity: dict[PairingIdentity, list[ComparisonMetricInput]] = {}
    for metric in inputs.metrics:
        metrics_by_identity.setdefault(metric.identity, []).append(metric)

    diagnostics = list(inputs.diagnostics)
    pairs: list[dict[str, Any]] = []
    for pair in pairing.pairs:
        entries: list[dict[str, Any]] = []
        for metric in sorted(metrics_by_identity.get(pair.identity, []), key=lambda item: item.variable):
            percent = _percent_delta(metric.version2_value, metric.version1_value)
            status = _comparison_status(percent, thresholds)
            if percent is None:
                diagnostics.append(
                    ComparisonDiagnostic(
                        code="zero_version1_baseline",
                        message=(
                            f"{metric.variable} percentage is unavailable because the version1 value is zero"
                        ),
                        variable=metric.variable,
                        identity=metric.identity,
                    )
                )
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
        pairs.append(
            {
                "identity": _identity_to_dict(pair.identity),
                "version1": _report_variant_input(pair.version1),
                "version2": _report_variant_input(pair.version2),
                "metrics": entries,
            }
        )

    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": {
            "basis": "percent_regression",
            "warning_percent": thresholds.warning_percent,
            "failure_percent": thresholds.failure_percent,
        },
        "pairs": pairs,
        "diagnostics": [diagnostic.to_dict() for diagnostic in diagnostics],
    }


def write_diff_comparison_report(report: dict[str, Any], output_path: Path) -> Path:
    """Persist the authoritative comparison report as versioned JSON."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def render_diff_comparison_table(report: dict[str, Any]) -> str:
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
        table_rows = [
            (
                _format_metric_name(str(metric["variable"])),
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
                    ("Metric [mA]", "Version 2", "Version 1", "Δ (v2 − v1)", "Change", "Result"),
                    table_rows,
                ),
                "",
            )
        )
    return "\n".join(tables).rstrip() if tables else "No comparable metrics."


def _table_title(identity: dict[str, Any]) -> str:
    return "### {feature} — {config_path} — {firmware_role}".format(
        feature=identity.get("feature", "unknown feature"),
        config_path=identity.get("config_path", "unknown config"),
        firmware_role=identity.get("firmware_role", "unknown role"),
    )


def _format_metric_name(variable: str) -> str:
    """Make known current metric names concise without changing report keys."""

    return variable.removesuffix("_current_ma").replace("_", " ")


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


def _identity_to_dict(identity: PairingIdentity) -> dict[str, str]:
    return {
        "feature": identity.feature,
        "config_path": identity.config_path,
        "config_hash": identity.config_hash,
        "partition": identity.partition,
        "mode": identity.mode,
        "board": identity.board,
        "device": identity.device,
        "firmware_role": identity.firmware_role,
    }


def _report_variant_input(result: VariantResult) -> dict[str, Any]:
    return {
        "artifact_path": result.path.as_posix(),
        "provenance": result.payload.get("firmware", {}),
    }


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
    if variable == "sleep_current_ma":
        summaries = payload.get("result_summary")
        if summaries is None:
            return "missing_interval_summary"
        intervals = [item for item in summaries if item.get("meas_type") == "Interval"]
        if not intervals:
            return "missing_interval_summary"
        if intervals[0].get("i_mean_ma") is None:
            return "null_metric"
        return None

    result_metric = payload["result_metric"]
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
    summaries = payload.get("result_summary")
    if summaries is None:
        return None
    if not isinstance(summaries, list):
        raise ResultMetricParseError("measurement result result_summary must be a list")
    intervals: list[dict[str, Any]] = []
    for index, summary in enumerate(summaries, start=1):
        if not isinstance(summary, dict):
            raise ResultMetricParseError(f"result_summary entry {index} must be an object")
        if summary.get("meas_type") == "Interval":
            intervals.append(summary)
    if not intervals:
        return None
    if len(intervals) != 1:
        raise ResultMetricParseError("result_summary has multiple Interval entries")
    return _optional_current(intervals[0], "i_mean_ma")


def _optional_current(payload: dict[str, Any], field: str) -> float | None:
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


def _load_firmware_roles(plan_path: Path) -> dict[tuple[str, str, str, str], PlannedJobIdentity]:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DifferentialPairingError(f"cannot read paired plan: {plan_path}") from exc
    except json.JSONDecodeError as exc:
        raise DifferentialPairingError(f"paired plan is not valid JSON: {plan_path}") from exc

    jobs = plan.get("jobs") if isinstance(plan, dict) else None
    if not isinstance(jobs, list):
        raise DifferentialPairingError("paired plan must contain a jobs list")

    roles: dict[tuple[str, str, str, str], PlannedJobIdentity] = {}
    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise DifferentialPairingError(f"paired plan job {index} must be an object")
        key = _plan_result_key(job, source=f"paired plan job {index}")
        role = _required_text(job, "firmware_role", source=f"paired plan job {index}")
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
    roles: dict[tuple[str, str, str, str], PlannedJobIdentity],
) -> list[VariantResult]:
    if not root.is_dir():
        raise DifferentialPairingError(f"{variant} result root does not exist: {root}")

    results: list[VariantResult] = []
    for path in sorted(root.rglob("*.json")):
        payload = _load_result_payload(path)
        if payload is None:
            continue
        result_key = _result_key(payload, source=str(path))
        planned_identity = roles.get(result_key)
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
        firmware = _required_object(payload, "firmware", source=str(path))
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


def _plan_result_key(job: dict[str, Any], *, source: str) -> tuple[str, str, str, str]:
    return (
        _required_text(job, "feature", source=source),
        _normalised_path(_required_text(job, "config_path", source=source)),
        _required_text(job, "config_hash", source=source),
        _required_text(job, "mode", source=source),
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
    )

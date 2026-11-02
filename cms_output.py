"""Rich-backed primitives for CMS command-line output."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Sequence
from typing import Any, TextIO

from rich.console import Console, RenderableType
from rich.markup import escape
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from cms_colors import COLOR_CHOICES, ROLE_STYLES, state_role
from cms_protocol import (
    BenchHealthReport,
    BenchStatus,
    MeasurementEventPage,
    MeasurementResult,
    MeasurementSummary,
)

CMS_THEME = Theme(ROLE_STYLES)


def create_console(mode: str = "auto", stream: TextIO | None = None) -> Console:
    """Create a Rich console using the CLI's ``auto|always|never`` color mode."""

    if mode not in COLOR_CHOICES:
        raise ValueError(f"color mode must be one of: {', '.join(COLOR_CHOICES)}")

    options: dict[str, Any] = {}
    if mode == "always":
        options.update(force_terminal=True, no_color=False, color_system="standard")
    elif mode == "never":
        options["no_color"] = True

    return Console(
        file=stream if stream is not None else sys.stdout,
        markup=False,
        soft_wrap=True,
        theme=CMS_THEME,
        **options,
    )


def create_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[RenderableType]] = (),
    *,
    title: str | None = None,
) -> Table:
    """Build a consistently styled table for terminal summaries."""

    table = Table(title=title, header_style="heading", show_header=bool(headers))
    for header in headers:
        table.add_column(header, overflow="fold")
    for row in rows:
        table.add_row(*row)
    return table


def print_bench_status(
    status: BenchStatus,
    *,
    color_mode: str = "auto",
    console: Console | None = None,
) -> None:
    """Render the current bench state and any active measurement."""

    console = console or create_console(color_mode)
    heading = Text("BENCH ")
    state = str(getattr(status.state, "value", status.state))
    heading.append(state.upper(), style=state_role(state))
    console.print(heading)
    _print_labeled_value(console, "bench", status.identity.bench_id)
    if status.active_measurement_id:
        _print_labeled_value(console, "active measurement", status.active_measurement_id)
    if status.detail:
        console.print(Text(f"  {status.detail}"))


def print_test_catalog(
    payload: dict[str, Any],
    *,
    color_mode: str = "auto",
    console: Console | None = None,
) -> None:
    """Render available modes and feature configurations in a readable list."""

    console = console or create_console(color_mode)
    modes = [mode for mode in payload.get("modes", []) if mode != "debug"]
    modes_line = Text()
    modes_line.append("modes: ", style="heading")
    modes_line.append(", ".join(str(mode) for mode in modes) or "none")
    console.print(modes_line)

    features = [item for item in payload.get("features", []) if isinstance(item, dict)]
    rows = [
        (str(feature.get("feature", "")), ", ".join(feature.get("configs", [])))
        for feature in features
    ]
    if rows:
        console.print(
            create_table(("feature", "configurations"), rows, title="Available tests")
        )
    else:
        console.print("no tests available")


def print_measurement_result(
    result: MeasurementResult,
    *,
    show_progress: bool = True,
    color_mode: str = "auto",
    console: Console | None = None,
) -> None:
    """Render a submitted or completed measurement and its artifacts."""

    console = console or create_console(color_mode)
    state = str(getattr(result.state, "value", result.state))
    heading = Text("MEASUREMENT ")
    heading.append(state.upper(), style=state_role(state))
    if show_progress:
        heading.append(f"  {result.progress_percent}%", style="dim")
    console.print(heading)
    _print_labeled_value(console, "id", result.measurement_id)
    if result.identity is not None:
        _print_labeled_value(console, "run", result.identity.run_name)
        _print_labeled_value(console, "created", result.identity.created_at.isoformat())
    if result.current_step:
        _print_labeled_value(console, "step", result.current_step)
    if result.message:
        console.print(Text(result.message))
    if result.artifacts:
        rows = [
            (artifact.name, artifact.url or str(artifact.path or ""))
            for artifact in result.artifacts
        ]
        console.print(create_table(("artifact", "location"), rows, title="Artifacts"))


def print_measurement_summary(
    summary: MeasurementSummary,
    *,
    show_header: bool = True,
    show_failed_tests: bool = True,
    elapsed_seconds: float | None = None,
    color_mode: str = "auto",
    console: Console | None = None,
) -> None:
    """Render test counts and relevant progress for result and watch commands."""

    console = console or create_console(color_mode)
    if show_header:
        state = str(getattr(summary.state, "value", summary.state))
        heading = Text("MEASUREMENT ")
        heading.append(state.upper(), style=state_role(state))
        console.print(heading)
        _print_labeled_value(console, "id", summary.measurement_id)

    counts = summary.counts
    counts_line = Text()
    counts_line.append("tests: ", style="heading")
    counts_line.append(f"{counts.total} total | {counts.completed} completed | ")
    counts_line.append(f"PASS {counts.passed}", style="passed")
    counts_line.append(" | ")
    counts_line.append(f"FAIL {counts.failed}", style="failed")
    counts_line.append(" | ")
    counts_line.append(f"SKIP {counts.skipped}", style="skipped")
    counts_line.append(f" | {counts.remaining} remaining")
    if elapsed_seconds is not None:
        counts_line.append(f" | elapsed {_format_elapsed(elapsed_seconds)}", style="dim")
    console.print(counts_line)

    if summary.current_test is not None:
        current = summary.current_test
        _print_labeled_value(
            console,
            "current",
            f"{current.ordinal}/{current.total_jobs} {current.feature} / {current.config}",
        )
    if show_failed_tests and summary.failed_tests:
        console.print(Text("failed tests", style="heading"))
        for failed in summary.failed_tests:
            console.print(
                Text(
                    f"  {failed.ordinal}/{failed.total_jobs} {failed.feature} / {failed.config}",
                    style="failed",
                )
            )


class TestExecutionRenderer:
    """Render live test progress and useful diagnostics without changing the run."""

    def __init__(
        self,
        console: Console,
        *,
        verbose: bool = False,
        live_progress: bool | None = None,
    ):
        self.console = console
        self.verbose = verbose
        self.live_progress_enabled = (
            console.is_terminal if live_progress is None else live_progress
        )
        self.progress = Progress(
            SpinnerColumn(style="running"),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )
        self._task_id: int | None = None
        self._last_summary_key: tuple[Any, ...] | None = None
        self._summary: MeasurementSummary | None = None
        self._seen_diagnostics: set[str] = set()
        self._traceback_lines: list[str] = []
        self._used_live_progress = False
        self._last_elapsed_bucket = -1
        self._stderr_lines_shown = 0
        self._stderr_suppressed_notice = False

    def start(self) -> None:
        if self.live_progress_enabled:
            self.progress.start()
            self._task_id = self.progress.add_task(
                "SETUP - Preparing test run",
                total=None,
            )
            self._used_live_progress = True

    def render_summary(
        self,
        summary: MeasurementSummary,
        *,
        elapsed_seconds: float,
    ) -> None:
        self._summary = summary
        key = (summary.state, summary.counts, summary.current_test, summary.failed_tests)
        if self._task_id is not None:
            self.progress.update(
                self._task_id,
                description=escape(self._progress_description(summary)),
                total=summary.counts.total or None,
                completed=summary.counts.completed,
            )
        elif (
            key != self._last_summary_key
            or int(elapsed_seconds // 30) > self._last_elapsed_bucket
        ):
            print_measurement_summary(
                summary,
                elapsed_seconds=elapsed_seconds,
                console=self.console,
            )
            self._last_elapsed_bucket = int(elapsed_seconds // 30)
        self._last_summary_key = key

    def render_events(self, page: MeasurementEventPage) -> None:
        for event in page.events:
            if self.verbose:
                self._print_verbose_event(event)
            elif event.event_type == "console":
                self._print_concise_console_event(event.details)
            elif event.event_type == "lifecycle":
                self._print_lifecycle_event(event.details)

    def render_truncation(self, oldest_cursor: int) -> None:
        self._emit(
            "WARN",
            f"verbose console output was truncated; continuing from cursor {oldest_cursor}",
            "warning",
        )

    def stop(self) -> None:
        if self._task_id is not None:
            self.progress.stop()
            self._task_id = None

    def print_final_summary(self, *, elapsed_seconds: float) -> None:
        if self._used_live_progress and self._summary is not None:
            print_measurement_summary(
                self._summary,
                elapsed_seconds=elapsed_seconds,
                console=self.console,
            )

    def _progress_description(self, summary: MeasurementSummary) -> str:
        counts = summary.counts
        if summary.state.value in {
            "passed",
            "failed",
            "cancelled",
            "rejected",
            "interrupted",
        }:
            return f"RESULT {summary.state.value.upper()} - {counts.completed}/{counts.total}"
        if summary.current_test is not None:
            current = summary.current_test
            return (
                f"EXECUTION - {current.ordinal}/{current.total_jobs} "
                f"{current.feature} / {current.config}"
            )
        if counts.total and counts.completed >= counts.total:
            return f"TEARDOWN - Finalizing results ({counts.completed}/{counts.total})"
        if counts.total:
            return f"SETUP - Preparing tests ({counts.completed}/{counts.total})"
        return "SETUP - Preparing test run"

    def _print_verbose_event(self, event: Any) -> None:
        timestamp = event.timestamp.isoformat()
        if event.event_type == "console":
            source = str(event.details.get("source", "console"))
            variant = str(event.details.get("variant", ""))
            label = f"{variant} {source}".strip()
            for line in str(event.details.get("text", "")).splitlines() or [""]:
                self.console.print(
                    Text.assemble((timestamp, "dim"), (f" [{label}] ", "info"), line)
                )
            return

        state = str(event.details.get("state", event.event_type))
        details = json.dumps(event.details, sort_keys=True, default=str)
        self.console.print(
            Text.assemble(
                (timestamp, "dim"),
                (" [event] ", "info"),
                (state, state_role(state)),
                (f" {details}",),
            )
        )

    def _print_lifecycle_event(self, details: dict[str, Any]) -> None:
        state = str(details.get("state", "event"))
        if state == "process_started":
            self._emit("EXECUTION", "Test orchestrator started", "running")
        elif state == "process_completed":
            code = details.get("returncode", "unknown")
            self._emit("EXECUTION", f"Test orchestrator finished (exit {code})", "info")
        elif state == "process_timed_out":
            self._emit("FAIL", "Test orchestrator timed out", "failed")
        elif state == "process_cancelled":
            self._emit("WARN", "Test orchestrator was cancelled", "warning")
        elif state in {"console_truncated", "log_truncated"}:
            self._emit("WARN", "Some diagnostic output was truncated", "warning")
        elif state == "differential_setup_failure":
            variant = str(details.get("variant", ""))
            phase = str(details.get("phase", "setup"))
            message = str(details.get("message", ""))
            prefix = f"{variant} " if variant else ""
            self._emit("FAIL", f"{prefix}{phase} failed: {message}", "failed")

    def _print_concise_console_event(self, details: dict[str, Any]) -> None:
        source = str(details.get("source", "stdout"))
        for raw_line in str(details.get("text", "")).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if self._traceback_lines:
                if raw_line[:1].isspace():
                    self._traceback_lines.append(line)
                    continue
                self._traceback_lines.clear()
                self._emit_failure_detail(line)
                continue
            if line.startswith("Traceback (most recent call last):"):
                self._traceback_lines = [line]
                continue
            if self._print_orchestrator_line(line):
                continue
            if self._print_phase_line(line):
                continue
            if self._print_diagnostic_line(line):
                continue
            if source == "stderr" and self._is_unclassified_stderr(line):
                if self._stderr_lines_shown < 20:
                    self._emit_unique("STDERR", line, "warning")
                    self._stderr_lines_shown += 1
                elif not self._stderr_suppressed_notice:
                    self._emit(
                        "WARN",
                        "Additional stderr lines are hidden; use --verbose to show the full output",
                        "warning",
                    )
                    self._stderr_suppressed_notice = True

    @staticmethod
    def _is_unclassified_stderr(line: str) -> bool:
        if re.match(r"^(?:INFO|DEBUG)\b", line):
            return False
        if re.match(r"^(?:collected \d+ items?|=+|[-_]{5,})", line, re.IGNORECASE):
            return False
        if re.search(r"\b(?:PASSED|SKIPPED|XFAIL|XPASS)\b", line):
            return False
        return True

    def _print_orchestrator_line(self, line: str) -> bool:
        if line.startswith("Running "):
            self._emit("EXECUTION", line.removeprefix("Running ").strip(), "running")
            return True
        if "Retrying " in line and "retryable startup failure" in line:
            self._emit("RETRY", line.split("Retrying ", 1)[1], "warning")
            return True
        if line.startswith("Passed "):
            self._emit("TEARDOWN PASS", line.removeprefix("Passed ").strip(), "passed")
            return True
        if line.startswith("Failed "):
            self._emit("TEARDOWN FAIL", line.removeprefix("Failed ").strip(), "failed")
            return True
        if line.startswith("Stopping early after "):
            self._emit("WARN", line, "warning")
            return True
        return False

    def _print_phase_line(self, line: str) -> bool:
        setup = re.search(
            r"\b(SETUP\s+(?:START|COMPLETE|SKIP|TIMEOUT):)\s*(.*)$",
            line,
            re.IGNORECASE,
        )
        if setup is not None:
            label = setup.group(1).upper().replace(":", "")
            role = "warning" if "SKIP" in label or "TIMEOUT" in label else "setup"
            self._emit(label, setup.group(2), role)
            return True
        if "TEARDOWN" in line.upper():
            self._emit("TEARDOWN", line, "setup")
            return True
        return False

    def _print_diagnostic_line(self, line: str) -> bool:
        failed = re.match(r"^FAILED\s+(.+?)(?:\s+-\s+(.+))?$", line)
        if failed is not None:
            error = failed.group(2) or "pytest reported a failure"
            self._emit_unique("FAIL", f"{failed.group(1)}: {error}", "failed")
            return True
        exception = re.match(
            r"^E\s+((?:[A-Za-z_][\w.]*)(?:Error|Exception|Warning|Failure)\b.*)$",
            line,
        )
        if exception is not None:
            self._emit_failure_detail(exception.group(1))
            return True
        severity = re.search(
            r"\b(CRITICAL|ERROR|WARNING)\b\s*:?\s*(.*)$",
            line,
            re.IGNORECASE,
        )
        if severity is None:
            return False
        level = severity.group(1).upper()
        message = severity.group(2).strip() or line
        is_error = level in {"CRITICAL", "ERROR"}
        self._emit_unique("FAIL" if is_error else "WARN", message, "failed" if is_error else "warning")
        return True

    def _emit_failure_detail(self, exception: str) -> None:
        context = ""
        if self._summary is not None and self._summary.failed_tests:
            test = self._summary.failed_tests[-1]
            context = f"{test.feature} / {test.config}: "
        self._emit_unique("FAIL", context + exception, "failed")

    def _emit_unique(self, label: str, message: str, role: str) -> None:
        normalized = re.sub(r"\s+", " ", message).strip()
        key = f"{label}:{normalized}"
        if not normalized or key in self._seen_diagnostics:
            return
        self._seen_diagnostics.add(key)
        self._emit(label, normalized, role)

    def _emit(self, label: str, message: str, role: str) -> None:
        compact = message if len(message) <= 300 else message[:297] + "..."
        self.console.print(Text.assemble((f"{label} ", role), (compact,)))


def print_saved_measurements(
    measurements: list[dict[str, Any]],
    *,
    color_mode: str = "auto",
    console: Console | None = None,
) -> None:
    """Render retained measurement records as a compact table."""

    console = console or create_console(color_mode)
    if not measurements:
        console.print("no recoverable measurements")
        return

    rows = []
    for item in measurements:
        state = str(item.get("state", "unknown")) or "unknown"
        rows.append(
            (
                str(item.get("measurement_id", "")),
                Text(state, style=state_role(state)),
                str(int(item.get("artifact_count", 0))),
                "manifest" if bool(item.get("has_manifest", False)) else "files-only",
                str(item.get("last_modified_at", "")),
            )
        )
    console.print(
        create_table(
            ("measurement", "state", "artifacts", "storage", "modified"),
            rows,
            title="Recoverable measurements",
        )
    )


def _print_labeled_value(console: Console, label: str, value: str) -> None:
    line = Text()
    line.append(f"{label}: ", style="heading")
    line.append(value)
    console.print(line)


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def print_health_report(
    report: BenchHealthReport,
    *,
    verbose: bool = False,
    color_mode: str = "auto",
    console: Console | None = None,
) -> None:
    """Render a concise bench health summary, with full diagnostics on request."""

    console = console or create_console(color_mode)
    counts = {"pass": 0, "warn": 0, "fail": 0}
    for check in report.checks:
        status = str(getattr(check.status, "value", check.status)).lower()
        if status in counts:
            counts[status] += 1

    if counts["fail"] or report.state.value == "error":
        overall_label, overall_role = "FAIL", "failed"
    elif counts["warn"]:
        overall_label, overall_role = "WARN", "warning"
    else:
        overall_label, overall_role = "PASS", "passed"

    headline = Text()
    headline.append("HEALTH ")
    headline.append(overall_label, style=overall_role)
    console.print(headline)

    bench_line = Text()
    bench_line.append("bench: ", style="heading")
    bench_line.append(report.identity.bench_id or "unknown")
    bench_line.append(" | state: ")
    bench_line.append(
        str(getattr(report.state, "value", report.state)),
        style=state_role(report.state),
    )
    console.print(bench_line)

    hardware = _health_hardware_summary(report)
    hardware_line = Text()
    hardware_line.append("hardware: ", style="heading")
    hardware_line.append(hardware)
    console.print(hardware_line)
    if verbose:
        detected = report.detected_identity
        if detected is None:
            console.print("  detected DUT identity: unavailable")
        else:
            vcom = (
                "unknown"
                if detected.vcom_supported is None
                else str(detected.vcom_supported).lower()
            )
            console.print(f"  radio board serial: {detected.radio_board_serial_number}")
            console.print(
                "  target device: "
                f"{detected.target_device} (canonical {detected.canonical_target_device})"
            )
            console.print(f"  DUT J-Link serial: {detected.dut_jlink_serial}")
            console.print(f"  VCOM supported: {vcom}")

    counts_line = Text()
    counts_line.append(f"checks: {len(report.checks)} total")
    for label, role in (("pass", "passed"), ("warn", "warning"), ("fail", "failed")):
        counts_line.append(" | ")
        counts_line.append(f"{label.upper()} {counts[label]}", style=role)
    console.print(counts_line)

    visible_checks = (
        report.checks
        if verbose
        else [
            check
            for check in report.checks
            if str(getattr(check.status, "value", check.status)).lower() in {"warn", "fail"}
        ]
    )
    for check in visible_checks:
        status = str(getattr(check.status, "value", check.status)).lower()
        role = state_role(status)
        check_line = Text("  ")
        check_line.append(status.upper(), style=role)
        check_line.append(f" {check.name}: {check.summary}")
        console.print(check_line)
        if verbose and check.detail:
            detail_line = Text(f"     {check.detail}")
            detail_line.stylize("dim", 0, 5)
            console.print(detail_line)


def _health_hardware_summary(report: BenchHealthReport) -> str:
    detected = report.detected_identity
    if detected is not None:
        board = detected.radio_board_part_number
        if detected.radio_board_revision:
            board = f"{board} Rev. {detected.radio_board_revision}" if board else ""
        device = detected.target_device
    else:
        board = report.identity.board
        device = report.identity.device

    parts = [part for part in (board, device) if part]
    return " / ".join(parts) if parts else "identity unavailable"

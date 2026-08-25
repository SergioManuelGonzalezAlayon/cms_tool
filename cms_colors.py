"""Shared ANSI color formatting for human CMS command output."""

from __future__ import annotations

import sys
from typing import TextIO


COLOR_CHOICES = ("auto", "always", "never")

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "magenta": "\033[35m",
    "red": "\033[31m",
    "yellow": "\033[33m",
}

_ROLE_COLORS = {
    "cancelled": "magenta",
    "error": "red",
    "failed": "red",
    "heading": "bold",
    "info": "blue",
    "passed": "green",
    "rejected": "red",
    "running": "cyan",
    "setup": "cyan",
    "skipped": "yellow",
    "warning": "yellow",
}

_STATE_ROLES = {
    "accepted": "running",
    "busy": "running",
    "cancelled": "cancelled",
    "collecting_artifacts": "running",
    "error": "error",
    "fail": "failed",
    "failed": "failed",
    "idle": "passed",
    "interrupted": "warning",
    "offline": "warning",
    "passed": "passed",
    "pass": "passed",
    "pending": "running",
    "process_cancelled": "cancelled",
    "process_cancellation_requested": "cancelled",
    "process_completed": "passed",
    "process_started": "running",
    "process_timed_out": "failed",
    "rejected": "rejected",
    "running": "running",
    "skipped": "skipped",
    "testing": "running",
    "warn": "warning",
    "warning": "warning",
}


class ColorFormatter:
    """Apply ANSI colors only when enabled for human-readable output."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def color(self, text: object, role: str) -> str:
        value = str(text)
        if not self.enabled:
            return value
        color = _ROLE_COLORS.get(role, role)
        prefix = _ANSI.get(color)
        if prefix is None:
            return value
        return f"{prefix}{value}{_ANSI['reset']}"

    def state(self, state: object) -> str:
        value = str(getattr(state, "value", state))
        return self.color(value, state_role(value))


def color_formatter(mode: str = "auto", stream: TextIO | None = None) -> ColorFormatter:
    """Return a formatter for ``auto|always|never`` color behavior."""

    if mode not in COLOR_CHOICES:
        raise ValueError(f"color mode must be one of: {', '.join(COLOR_CHOICES)}")
    if mode == "always":
        return ColorFormatter(True)
    if mode == "never":
        return ColorFormatter(False)
    output = stream if stream is not None else sys.stdout
    return ColorFormatter(bool(getattr(output, "isatty", lambda: False)()))


def state_role(state: object) -> str:
    value = str(getattr(state, "value", state)).strip().lower().replace("-", "_")
    return _STATE_ROLES.get(value, "info")

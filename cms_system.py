"""Linux process-group helpers for CMS Bench measurements.

CMS Benches run on Raspberry Pi Linux. This module keeps `/proc` inspection and
process-group termination out of the daemon's measurement orchestration.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path


PROCESS_TERMINATE_GRACE_S = 5.0
PROCESS_GROUP_TERMINATE_TIMEOUT_S = 5.0
PROCESS_GROUP_KILL_TIMEOUT_S = 5.0
PROCESS_GROUP_POLL_INTERVAL_S = 0.05


def measurement_process_group_ids(runtime_dir: Path) -> tuple[int, ...]:
    """Read process-group ownership records from a measurement's event files."""

    process_group_ids: set[int] = set()
    for events_path in runtime_dir.rglob("events.jsonl"):
        try:
            lines = events_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise RuntimeError(
                f"cannot verify measurement process groups from {events_path}: {exc}"
            ) from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"cannot verify measurement process groups: invalid event at "
                    f"{events_path}:{line_number}"
                ) from exc
            if not isinstance(record, dict):
                raise RuntimeError(
                    f"cannot verify measurement process groups: invalid event at "
                    f"{events_path}:{line_number}"
                )
            if record.get("state") != "process_started":
                continue
            # Each managed child starts a new session, so PID is also its PGID.
            group_id = record.get("process_group_id") or record.get("pid")
            if (
                not isinstance(group_id, int)
                or isinstance(group_id, bool)
                or group_id <= 0
            ):
                raise RuntimeError(
                    f"cannot verify measurement process group from {events_path}:{line_number}"
                )
            process_group_ids.add(group_id)
    return tuple(sorted(process_group_ids))


def active_process_group_pids(
    process_group_id: int,
    *,
    proc_root: Path = Path("/proc"),
) -> tuple[int, ...]:
    """Return live members of a Linux process group, excluding zombies."""

    try:
        entries = tuple(proc_root.iterdir())
    except OSError as exc:
        raise RuntimeError(f"cannot inspect Linux process table at {proc_root}: {exc}") from exc

    active_pids: list[int] = []
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            # The process exited between listing /proc and reading its stat.
            continue
        except OSError as exc:
            raise RuntimeError(
                f"cannot inspect process {entry.name} while verifying group {process_group_id}: {exc}"
            ) from exc

        closing_paren = stat.rfind(")")
        if closing_paren < 0:
            raise RuntimeError(f"invalid /proc/{entry.name}/stat while verifying process groups")
        fields = stat[closing_paren + 1 :].split()
        if len(fields) < 3:
            raise RuntimeError(f"incomplete /proc/{entry.name}/stat while verifying process groups")
        state = fields[0].upper()
        try:
            process_group = int(fields[2])
        except ValueError as exc:
            raise RuntimeError(
                f"invalid process group in /proc/{entry.name}/stat"
            ) from exc
        if process_group == process_group_id and state not in {"Z", "X"}:
            active_pids.append(int(entry.name))
    return tuple(sorted(active_pids))


def _wait_for_process_group_exit(
    process_group_id: int,
    timeout_s: float,
) -> tuple[int, ...]:
    deadline = time.monotonic() + timeout_s
    while True:
        active_pids = active_process_group_pids(process_group_id)
        if not active_pids:
            return ()
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            return active_pids
        time.sleep(min(PROCESS_GROUP_POLL_INTERVAL_S, remaining_s))


def _stop_process_group(
    process_group_id: int,
    *,
    terminate_timeout_s: float = PROCESS_GROUP_TERMINATE_TIMEOUT_S,
    kill_timeout_s: float = PROCESS_GROUP_KILL_TIMEOUT_S,
) -> tuple[int, ...]:
    """Give leftover group members TERM, then KILL, and verify the group is idle."""

    initial_pids = active_process_group_pids(process_group_id)
    if not initial_pids:
        return ()

    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise RuntimeError(
            f"cannot send SIGTERM to process group {process_group_id}: {exc}"
        ) from exc
    remaining_pids = _wait_for_process_group_exit(process_group_id, terminate_timeout_s)
    if not remaining_pids:
        return initial_pids

    try:
        os.killpg(process_group_id, getattr(signal, "SIGKILL", 9))
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise RuntimeError(
            f"cannot send SIGKILL to process group {process_group_id}: {exc}"
        ) from exc
    remaining_pids = _wait_for_process_group_exit(process_group_id, kill_timeout_s)
    if remaining_pids:
        raise RuntimeError(
            f"process group {process_group_id} still has active process(es) after SIGKILL: "
            + ", ".join(str(pid) for pid in remaining_pids)
        )
    return initial_pids


def cleanup_measurement_process_groups(
    runtime_dir: Path,
) -> tuple[tuple[int, ...], dict[int, tuple[int, ...]]]:
    """Stop all process groups recorded as owned by one measurement."""

    process_group_ids = measurement_process_group_ids(runtime_dir)
    terminated_pids = {
        process_group_id: _stop_process_group(
            process_group_id,
            terminate_timeout_s=PROCESS_GROUP_TERMINATE_TIMEOUT_S,
            kill_timeout_s=PROCESS_GROUP_KILL_TIMEOUT_S,
        )
        for process_group_id in process_group_ids
    }
    return process_group_ids, terminated_pids


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Stop and reap a managed Linux session, including surviving descendants."""

    def reap_with_timeout() -> None:
        try:
            process.wait(timeout=PROCESS_TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"managed child process {process.pid} did not exit after SIGKILL"
            ) from exc

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        reap_with_timeout()
        return

    try:
        process.wait(timeout=PROCESS_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process.pid, getattr(signal, "SIGKILL", 9))
    except ProcessLookupError:
        reap_with_timeout()
        return
    # The session leader may exit while descendants still hold inherited pipes.
    reap_with_timeout()

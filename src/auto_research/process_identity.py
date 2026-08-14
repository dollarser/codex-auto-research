"""Linux process identity and bounded termination helpers."""

from __future__ import annotations

import os
import signal
import time
from typing import Literal

ProcessIdentityState = Literal["alive", "dead", "reused", "unverifiable"]


def process_start_ticks(pid: int) -> int | None:
    """Return Linux ``/proc`` start ticks, which disambiguate PID reuse."""
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as stream:
            stat = stream.read()
        fields = stat[stat.rfind(")") + 2 :].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def process_matches(pid: object, start_ticks: object) -> bool:
    """Return whether both PID and immutable process start time still match."""
    return process_identity_state(pid, start_ticks) == "alive"


def process_identity_state(pid: object, start_ticks: object) -> ProcessIdentityState:
    """Classify a durable Linux process identity without guessing on errors."""
    try:
        parsed_pid = int(pid)
        parsed_start = int(start_ticks)
        if parsed_pid <= 0 or parsed_start <= 0:
            return "unverifiable"
        os.kill(parsed_pid, 0)
    except ProcessLookupError:
        return "dead"
    except (TypeError, ValueError, PermissionError):
        return "unverifiable"
    current_start = process_start_ticks(parsed_pid)
    if current_start is None:
        return "unverifiable"
    return "alive" if current_start == parsed_start else "reused"


def terminate_process_group(
    pid: object,
    start_ticks: object,
    *,
    grace_s: float = 10.0,
) -> bool:
    """Terminate one verified process group and confirm that its leader exited."""
    try:
        parsed_pid = int(pid)
    except (TypeError, ValueError):
        return True
    try:
        os.kill(parsed_pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, ValueError):
        return False
    try:
        parsed_start = int(start_ticks)
    except (TypeError, ValueError):
        # A live process without a durable identity must never be reported as
        # stopped: killing it could hit a reused PID, while returning success
        # would falsely finalize the run.
        return False
    if process_start_ticks(parsed_pid) != parsed_start:
        # The recorded process is gone and this PID now belongs to another
        # process. The intended target is therefore already stopped.
        return True
    try:
        os.killpg(parsed_pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not process_matches(parsed_pid, start_ticks):
            return True
        time.sleep(0.05)
    try:
        os.killpg(parsed_pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + min(grace_s, 5.0)
    while time.monotonic() < deadline:
        if not process_matches(parsed_pid, start_ticks):
            return True
        time.sleep(0.05)
    return not process_matches(parsed_pid, start_ticks)

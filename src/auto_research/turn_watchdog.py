"""Bounded Goal Turn waiting while experiment ownership remains live."""

from __future__ import annotations

import json
import time
from typing import Any

from .app_server import AppServerError, AppServerTimeout
from .ledger import read_json


def find_turn(thread: dict[str, Any], turn_id: str) -> dict[str, Any] | None:
    """Return one authoritative Turn snapshot from a thread/read response."""
    for turn in thread.get("turns", []):
        if isinstance(turn, dict) and turn.get("id") == turn_id:
            return turn
    return None


def turn_progress_signature(turn: dict[str, Any] | None) -> str | None:
    """Build a stable signature whose changes prove observable Turn progress."""
    if turn is None:
        return None
    return json.dumps(turn, ensure_ascii=False, sort_keys=True, default=repr)


def wait_turn_while_observing_runs(
    supervisor: Any, client: Any, thread_id: str, turn_id: str
) -> dict[str, Any]:
    """Wait until completion or a bounded period without observable progress."""
    deadline = time.monotonic() + supervisor.config.goal_turn_timeout_s
    last_progress: str | None = None
    has_progress_baseline = False
    while True:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            return client.wait_turn(
                thread_id, turn_id, timeout_s=min(5.0, remaining)
            )
        except AppServerTimeout:
            supervisor._apply_goal_status_request(client, thread_id)
            snapshot = client.read_thread(thread_id, include_turns=True)
            turn = find_turn(snapshot, turn_id)
            if turn is not None and turn.get("status") != "inProgress":
                return turn
            progress = turn_progress_signature(turn)
            if not has_progress_baseline:
                last_progress = progress
                has_progress_baseline = True
            elif progress is not None and progress != last_progress:
                last_progress = progress
                deadline = time.monotonic() + supervisor.config.goal_turn_timeout_s
            supervisor._launch_or_observe_experiments(client, thread_id)

    recovery_turn_id = (read_json(supervisor.state_path, {}) or {}).get(
        "recovery_turn_id"
    )
    try:
        goal = client.get_goal(thread_id)
        if isinstance(goal, dict) and goal.get("status") == "active":
            client.set_goal_status(thread_id, "paused")
        client.interrupt_turn(thread_id, turn_id)
    except (AppServerError, RuntimeError) as exc:
        supervisor._write_state(
            state="NEEDS_USER",
            error=(
                f"Turn {turn_id} made no observable progress for "
                f"{supervisor.config.goal_turn_timeout_s}s "
                f"and could not be interrupted: {type(exc).__name__}: {exc}"
            ),
        )
        return {"id": turn_id, "status": "stalled"}
    if recovery_turn_id == turn_id:
        supervisor._write_state(
            state="NEEDS_USER",
            recovery_turn_id=None,
            error=f"repair Turn {turn_id} exceeded the no-progress deadline",
        )
        return {"id": turn_id, "status": "interrupted"}
    repair = supervisor._start_repair_turn(
        client,
        thread_id,
        reason=(
            f"Turn {turn_id} made no observable progress for "
            f"{supervisor.config.goal_turn_timeout_s}s"
        ),
    )
    if repair is None:
        return {"id": turn_id, "status": "interrupted"}
    return {
        "id": turn_id,
        "status": "interrupted",
        "supervisor_repair_turn_id": str(repair["id"]),
    }

"""Pure interpretations of App Server Goal, Turn, and experiment facts."""

from __future__ import annotations

import json
from typing import Any


def terminal_context(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Render a compact terminal notification; details remain in the run dir."""
    summary = {
        "run_id": result.get("run_id"),
        "status": result.get("status"),
        "result_dir": result.get("result_dir"),
    }
    payload = json.dumps(summary, ensure_ascii=False, indent=2)
    text = (
        "Auto Research experiment reached a terminal state. Read events, "
        "metrics.json, artifact validation, and logs from result_dir before "
        "deciding the next research action.\n\n" + payload
    )
    return [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }
    ]


def in_progress_turn(thread: dict[str, Any]) -> dict[str, Any] | None:
    """Return the newest in-progress Turn from an authoritative snapshot."""
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in reversed(turns):
        if isinstance(turn, dict) and turn.get("status") == "inProgress":
            return turn
    return None


def goal_stop_state(goal: dict[str, Any] | None) -> str | None:
    """Map live Goal status to the Supervisor's minimal stop categories."""
    status = goal.get("status") if isinstance(goal, dict) else None
    if status in {"complete", "blocked", "paused"}:
        return status
    if status in {"usageLimited", "budgetLimited"}:
        return "needs_user"
    return None

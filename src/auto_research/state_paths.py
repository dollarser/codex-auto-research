"""Canonical state paths keyed only by a Codex Thread id."""

from __future__ import annotations

import os
from pathlib import Path

THREAD_ID_ENV = "CODEX_THREAD_ID"


def supervisors_root(project_dir: str | Path) -> Path:
    return Path(project_dir).resolve() / "research" / "supervisors"


def validate_thread_id(value: str) -> str:
    thread_id = value.strip()
    if not thread_id:
        raise ValueError("thread_id must not be empty")
    if len(thread_id) > 200:
        raise ValueError("thread_id is too long")
    if thread_id in {".", ".."} or Path(thread_id).name != thread_id:
        raise ValueError("thread_id must be a single path-safe component")
    return thread_id


def resolve_thread_id(
    value: str | None = None,
    *,
    environment: dict[str, str] | None = None,
) -> str:
    """Resolve the exact task identity without searching persisted state."""
    env = os.environ if environment is None else environment
    environment_id = (env.get(THREAD_ID_ENV) or "").strip() or None
    explicit_id = value.strip() if isinstance(value, str) and value.strip() else None
    if explicit_id and environment_id and explicit_id != environment_id:
        raise ValueError("--thread-id does not match current CODEX_THREAD_ID")
    selected = explicit_id or environment_id
    if selected is None:
        raise ValueError(
            "thread id is required outside a Goal task; pass --thread-id"
        )
    return validate_thread_id(selected)


def thread_state_root(
    project_dir: str | Path,
    thread_id: str | None = None,
    *,
    environment: dict[str, str] | None = None,
) -> Path:
    """Return ``research/supervisors/<thread-id>`` for one exact Thread."""
    selected = (
        validate_thread_id(thread_id)
        if thread_id is not None
        else resolve_thread_id(environment=environment)
    )
    return supervisors_root(project_dir) / selected

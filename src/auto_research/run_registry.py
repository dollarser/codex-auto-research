"""Durable registry for Supervisor-owned experiment runs."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .ledger import read_json, write_json_atomic

REGISTRY_SCHEMA_VERSION = 1


class RegistryCorruptionError(RuntimeError):
    """Raised when durable run ownership cannot be trusted."""


def registry_path(state_root: str | Path) -> Path:
    return Path(state_root) / "supervisor" / "active_experiments.json"


def registry_lock_path(state_root: str | Path) -> Path:
    return Path(state_root) / "experiment_submission.lock"


@contextmanager
def registry_lock(state_root: str | Path) -> Iterator[None]:
    path = registry_lock_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    import fcntl

    with path.open("r+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_unlocked(state_root: str | Path) -> list[dict[str, Any]]:
    path = registry_path(state_root)
    if not path.exists():
        return []
    payload = read_json(path, None)
    if not isinstance(payload, dict):
        raise RegistryCorruptionError(f"invalid JSON object in {path}")
    if payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise RegistryCorruptionError(f"unsupported registry schema in {path}")
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise RegistryCorruptionError(f"registry runs must be a list in {path}")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in runs:
        if not isinstance(item, dict):
            raise RegistryCorruptionError(f"registry contains a non-object marker in {path}")
        run_id = item.get("run_id")
        thread_id = item.get("thread_id")
        if not isinstance(run_id, str) or not run_id:
            raise RegistryCorruptionError(f"registry marker has no run_id in {path}")
        if not isinstance(thread_id, str) or not thread_id:
            raise RegistryCorruptionError(f"registry marker has no thread_id in {path}")
        if run_id in seen:
            raise RegistryCorruptionError(f"duplicate run_id {run_id!r} in {path}")
        seen.add(run_id)
        validated.append(dict(item))
    return validated


def _write_unlocked(state_root: str | Path, runs: list[dict[str, Any]]) -> None:
    path = registry_path(state_root)
    if not runs:
        path.unlink(missing_ok=True)
        return
    write_json_atomic(
        path,
        {"schema_version": REGISTRY_SCHEMA_VERSION, "runs": runs},
    )


def list_active_runs(
    state_root: str | Path, *, thread_id: str | None = None
) -> list[dict[str, Any]]:
    with registry_lock(state_root):
        runs = _read_unlocked(state_root)
    if thread_id is None:
        return runs
    return [item for item in runs if item.get("thread_id") == thread_id]


def add_active_run(
    state_root: str | Path, *, run_id: str, thread_id: str
) -> dict[str, Any]:
    with registry_lock(state_root):
        runs = _read_unlocked(state_root)
        for item in runs:
            if item.get("run_id") == run_id:
                if item.get("thread_id") != thread_id:
                    raise RegistryCorruptionError(
                        f"run {run_id} is registered to another Thread"
                    )
                return dict(item)
        marker = {"run_id": run_id, "thread_id": thread_id}
        runs.append(marker)
        _write_unlocked(state_root, runs)
        return marker


def update_active_run(
    state_root: str | Path, run_id: str, **updates: Any
) -> dict[str, Any] | None:
    with registry_lock(state_root):
        runs = _read_unlocked(state_root)
        updated: dict[str, Any] | None = None
        for index, item in enumerate(runs):
            if item.get("run_id") != run_id:
                continue
            updated = {**item, **updates}
            runs[index] = updated
            break
        _write_unlocked(state_root, runs)
        return updated


def remove_active_run(state_root: str | Path, run_id: str) -> bool:
    with registry_lock(state_root):
        runs = _read_unlocked(state_root)
        retained = [item for item in runs if item.get("run_id") != run_id]
        removed = len(retained) != len(runs)
        _write_unlocked(state_root, retained)
        return removed

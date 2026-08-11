"""Experiment lifecycle MCP server.

The server exposes only deterministic experiment tools. Research decisions
remain in Codex Goal; this process owns durable run submission, result
retrieval, and cancellation.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import load_config
from .ledger import read_json, write_json_atomic
from .models import GoalSpec
from .research_session import read_bound_thread_id
from .runner import ExperimentRunner
from .supervisor import (
    is_supervisor_thread,
    pause_goal_for_experiment,
    supervisor_active_experiment_path,
)


class ExperimentService:
    def __init__(
        self,
        project_dir: str | Path | None = None,
        *,
        wake_launcher: Callable[..., dict[str, Any]] | None = None,
    ):
        self.project_dir = Path(
            project_dir or os.environ.get("AUTO_RESEARCH_PROJECT_DIR", ".")
        ).resolve()
        self.config = load_config(self.project_dir)
        self.runner = ExperimentRunner(
            self.project_dir / "research" / "runs", config=self.config
        )
        self.submission_lock_path = (
            self.project_dir / "research" / "experiment_submission.lock"
        )
        self.active_submission_path = (
            self.project_dir / "research" / "active_experiment.json"
        )
        if wake_launcher is None:
            from .wake_listener import spawn_wake_listener

            wake_launcher = spawn_wake_listener
        self.wake_launcher = wake_launcher

    @contextmanager
    def _submission_lock(self):
        self.submission_lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.submission_lock_path.touch(exist_ok=True)
        import fcntl

        with self.submission_lock_path.open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _active_run_id(
        self, marker_path: Path, *, thread_id: str | None = None
    ) -> str | None:
        active: dict[str, Any] = {}
        if marker_path.exists():
            try:
                parsed = json.loads(marker_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    active = parsed
                else:
                    raise TypeError("active_experiment.json must contain an object")
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                # A truncated marker is recoverable because run.json/events
                # remain the durable source of truth.
                marker_path.unlink(missing_ok=True)
        active_run_id = active.get("run_id")
        if isinstance(active_run_id, str):
            try:
                if self.runner.get_result(active_run_id) is None:
                    return active_run_id
            except (FileNotFoundError, ValueError):
                # The marker is a recovery hint, not a source of truth. A
                # truncated/manual marker must not take the MCP server down.
                pass
        if active_run_id:
            marker_path.unlink(missing_ok=True)

        # Recover the marker if the MCP process died immediately after submit.
        for run_file in sorted(
            self.project_dir.joinpath("research", "runs").glob("run-*/run.json")
        ):
            run = read_json(run_file, {}) or {}
            if (
                run.get("status") in {"SUBMITTED", "RUNNING"}
                and (thread_id is None or run.get("codex_thread_id") == thread_id)
            ):
                return run.get("run_id")
        return None

    def _release_active(self, run_id: str) -> None:
        for marker_path in (
            self.active_submission_path,
            supervisor_active_experiment_path(self.project_dir),
        ):
            active = read_json(marker_path, {}) or {}
            if active.get("run_id") == run_id:
                marker_path.unlink(missing_ok=True)

    def _worktree(self, value: str) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.project_dir / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.project_dir)
        except ValueError as exc:
            raise ValueError(
                "worktree must be inside AUTO_RESEARCH_PROJECT_DIR"
            ) from exc
        if not candidate.is_dir():
            raise ValueError(f"worktree does not exist: {candidate}")
        return candidate

    def _goal_contract_snapshot(self) -> dict[str, Any]:
        contract_path = self.project_dir / "research" / "goal_contract.json"
        goal_path = self.project_dir / "goal.json"
        path = contract_path if contract_path.exists() else goal_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data:
                raise ValueError("goal file must contain a non-empty JSON object")
            if path == contract_path:
                if data.get("schema_version") != 1:
                    raise ValueError("goal_contract.json must declare schema_version=1")
                if not isinstance(data.get("revision"), int) or data["revision"] < 1:
                    raise ValueError(
                        "goal_contract.json must declare a positive integer revision"
                    )
            goal = GoalSpec.from_dict(data)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid {path.name}; repair the goal contract before starting an experiment: {exc}"
            ) from exc
        canonical = json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return {
            "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "revision": data.get("revision"),
            "primary_metric": goal.primary_metric,
            "direction": goal.direction,
            "hard_requirements": goal.hard_requirements,
        }

    def start_experiment(
        self,
        idea_id: str,
        worktree: str,
        command: str,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        if not idea_id or not command:
            raise ValueError("idea_id and command are required")
        timeout_s = timeout_s or self.config.default_experiment_timeout_s
        if timeout_s <= 0 or timeout_s > 7 * 24 * 3600:
            raise ValueError("timeout_s must be between 1 and 604800")
        resolved = self._worktree(worktree)
        snapshot = self._goal_contract_snapshot()
        environment_thread_id = os.environ.get("CODEX_THREAD_ID") or None
        bound_thread_id = read_bound_thread_id(self.project_dir)
        if thread_id and environment_thread_id and thread_id != environment_thread_id:
            raise ValueError(
                "thread_id does not match the current CODEX_THREAD_ID; refusing "
                "to wake a task that did not submit this experiment"
            )
        selected_thread_id = thread_id or environment_thread_id or bound_thread_id
        supervisor_owned = is_supervisor_thread(
            self.project_dir, selected_thread_id
        )
        if (
            bound_thread_id
            and selected_thread_id
            and selected_thread_id != bound_thread_id
            and not supervisor_owned
        ):
            raise ValueError(
                f"project is bound to dedicated thread {bound_thread_id}, but the "
                f"experiment was submitted from {selected_thread_id}; open the "
                "dedicated task or remove the project binding intentionally"
            )
        thread_id = selected_thread_id
        wake_enabled = self.config.auto_wake and not supervisor_owned
        marker_path = (
            supervisor_active_experiment_path(self.project_dir)
            if supervisor_owned
            else self.active_submission_path
        )
        with self._submission_lock():
            if self.config.one_active_experiment:
                active_run_id = self._active_run_id(
                    marker_path,
                    thread_id=thread_id if supervisor_owned else None,
                )
                if active_run_id:
                    raise RuntimeError(
                        "one active experiment is allowed per project; "
                        f"run_id={active_run_id} is not terminal. End this turn and wait for its terminal event."
                    )
            run_id = self.runner.submit(
                idea_id,
                resolved,
                command,
                timeout_s,
                env,
                idempotency_key,
                goal_contract_digest=snapshot.get("digest"),
                goal_contract_revision=snapshot.get("revision"),
                hard_requirements_snapshot=snapshot.get("hard_requirements", []),
                wake_enabled=wake_enabled,
                codex_thread_id=thread_id,
                pause_goal_on_turn_end=bool(
                    not supervisor_owned
                    and environment_thread_id
                    and thread_id == environment_thread_id
                ),
                launch_worker=not supervisor_owned,
            )
            if self.config.one_active_experiment:
                write_json_atomic(marker_path, {"run_id": run_id})
        wake: dict[str, Any] = {"status": "DISABLED"}
        goal_pause: dict[str, Any] = {"status": "NOT_MANAGED"}
        if supervisor_owned:
            try:
                pause = pause_goal_for_experiment(
                    self.project_dir,
                    thread_id=str(thread_id),
                    run_id=run_id,
                )
                goal_pause = {"status": "PAUSED", **pause}
            except (OSError, RuntimeError, ValueError) as exc:
                goal_pause = {
                    "status": "ERROR",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                write_json_atomic(
                    self.runner.runs_dir / run_id / "goal-pause-error.json",
                    goal_pause,
                )
                raise RuntimeError(
                    f"experiment {run_id} started, but the native Goal could not "
                    "be paused; do not submit another experiment"
                ) from exc
        if wake_enabled:
            try:
                wake = self.wake_launcher(
                    self.project_dir,
                    run_id,
                    thread_id=thread_id,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                wake = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
                write_json_atomic(
                    self.runner.runs_dir / run_id / "wake-launch-error.json", wake
                )
        return {
            "run_id": run_id,
            "status": "RUNNING",
            "worktree": str(resolved),
            "wake_listener": wake,
            "goal_pause": goal_pause,
            "scheduler": "app_server_goal_runtime"
            if supervisor_owned
            else "goal_wake_listener",
        }

    def get_experiment_result(self, run_id: str) -> dict[str, Any]:
        if self.config.one_active_experiment:
            with self._submission_lock():
                result = self.runner.get_result(run_id)
                if result is None:
                    return {"run_id": run_id, "status": "RUNNING"}
                self._release_active(run_id)
        else:
            result = self.runner.get_result(run_id)
            if result is None:
                return {"run_id": run_id, "status": "RUNNING"}
        return result.to_dict()

    def cancel_experiment(self, run_id: str) -> dict[str, Any]:
        if self.config.one_active_experiment:
            with self._submission_lock():
                result = self.runner.cancel(run_id)
                self._release_active(run_id)
        else:
            result = self.runner.cancel(run_id)
        return result.to_dict()


def main() -> None:
    try:
        from fastmcp import FastMCP
    except ImportError as exc:
        raise SystemExit("Install MCP support with: uv sync --extra mcp") from exc

    service = ExperimentService()
    server = FastMCP("auto-research-experiments")

    @server.tool()
    def start_experiment(
        idea_id: str,
        worktree: str,
        command: str,
        timeout_s: int | None = None,
        idempotency_key: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist and start a detached experiment; returns immediately with run_id."""
        return service.start_experiment(
            idea_id,
            worktree,
            command,
            timeout_s,
            idempotency_key=idempotency_key,
            thread_id=thread_id,
        )

    @server.tool()
    def get_experiment_result(run_id: str) -> dict[str, Any]:
        """Read a terminal result, or return RUNNING if no terminal event exists."""
        return service.get_experiment_result(run_id)

    @server.tool()
    def cancel_experiment(run_id: str) -> dict[str, Any]:
        """Cancel a persisted run and write a terminal cancellation event."""
        return service.cancel_experiment(run_id)

    server.run()


if __name__ == "__main__":
    main()

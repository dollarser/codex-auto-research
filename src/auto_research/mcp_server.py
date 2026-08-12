"""Experiment lifecycle MCP server.

The server exposes deterministic experiment read/wait tools. Research decisions
remain in Codex Goal; durable submission uses ``auto-research submit`` CLI so
that it does not depend on an App Server MCP approval prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
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
from .state_paths import resolve_state_root


class ExperimentService:
    def __init__(self, project_dir: str | Path | None = None, state_root: str | Path | None = None):
        self.project_dir = Path(
            project_dir or os.environ.get("AUTO_RESEARCH_PROJECT_DIR", ".")
        ).resolve()
        self.state_root = resolve_state_root(self.project_dir, state_root or os.environ.get("AUTO_RESEARCH_STATE_ROOT"))
        self.config = load_config(self.project_dir)
        self.runner = ExperimentRunner(
            self.state_root / "runs", config=self.config
        )
        self.submission_lock_path = (
            self.state_root / "experiment_submission.lock"
        )
        self.active_submission_path = (
            self.state_root / "active_experiment.json"
        )

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
            self.state_root.joinpath("runs").glob("run-*/run.json")
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
            supervisor_active_experiment_path(self.project_dir, self.state_root),
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

    def submit_experiment(
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
        bound_thread_id = read_bound_thread_id(self.project_dir, self.state_root)
        if thread_id and environment_thread_id and thread_id != environment_thread_id:
            raise ValueError(
                "thread_id does not match the current CODEX_THREAD_ID; refusing "
                "to wake a task that did not submit this experiment"
            )
        selected_thread_id = thread_id or environment_thread_id or bound_thread_id
        supervisor_owned = is_supervisor_thread(self.project_dir, selected_thread_id, self.state_root)
        if not supervisor_owned:
            raise ValueError(
                "submit_experiment requires the project-bound managed Supervisor "
                "Goal task; experiment Workers may only be launched by Supervisor"
            )
        thread_id = selected_thread_id
        marker_path = supervisor_active_experiment_path(self.project_dir, self.state_root)
        with self._submission_lock():
            if self.config.one_active_experiment:
                active_run_id = self._active_run_id(
                    marker_path,
                    thread_id=thread_id,
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
                wake_enabled=False,
                codex_thread_id=thread_id,
                pause_goal_on_turn_end=False,
                launch_worker=False,
            )
            if self.config.one_active_experiment:
                write_json_atomic(
                    marker_path,
                    {
                        "run_id": run_id,
                        "thread_id": thread_id,
                        "wait_requested": False,
                    },
                )
        return {
            "run_id": run_id,
            "status": "SUBMITTED",
            "worktree": str(resolved),
            "worker_owner": "supervisor",
            "goal_pause": {
                "status": "NOT_REQUESTED",
                "continuation_allowed": True,
            },
            "scheduler": "app_server_supervisor",
        }

    def wait_for_experiment(
        self, run_id: str, thread_id: str | None = None
    ) -> dict[str, Any]:
        """Pause a managed Goal only after the Agent has no useful work left."""
        environment_thread_id = os.environ.get("CODEX_THREAD_ID") or None
        if thread_id and environment_thread_id and thread_id != environment_thread_id:
            raise ValueError("thread_id does not match the current CODEX_THREAD_ID")
        selected_thread_id = thread_id or environment_thread_id
        if not selected_thread_id or not is_supervisor_thread(
            self.project_dir, selected_thread_id, self.state_root
        ):
            raise ValueError(
                "wait_for_experiment requires the managed Supervisor Goal task"
            )
        result = self.runner.get_result(run_id)
        if result is not None:
            return {
                **result.to_dict(),
                "wait_handoff": "NOT_NEEDED",
            }
        with self._submission_lock():
            active = read_json(
                supervisor_active_experiment_path(self.project_dir, self.state_root), {}
            ) or {}
            if active.get("run_id") != run_id:
                raise ValueError("run_id is not the Supervisor's active experiment")
            handoff = pause_goal_for_experiment(
                self.project_dir,
                thread_id=selected_thread_id,
                run_id=run_id,
            )
        return {
            "run_id": run_id,
            "status": "WAITING",
            "wait_handoff": "PAUSED",
            "goal_pause": handoff,
        }

    def get_experiment_result(self, run_id: str) -> dict[str, Any]:
        if self.config.one_active_experiment:
            with self._submission_lock():
                result = self.runner.get_result(run_id)
                if result is None:
                    run = self.runner.get_run(run_id)
                    return {"run_id": run_id, "status": run.get("status")}
                self._release_active(run_id)
        else:
            result = self.runner.get_result(run_id)
            if result is None:
                run = self.runner.get_run(run_id)
                return {"run_id": run_id, "status": run.get("status")}
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
    def get_experiment_result(run_id: str) -> dict[str, Any]:
        """Read a terminal result, or return RUNNING if no terminal event exists."""
        return service.get_experiment_result(run_id)

    @server.tool()
    def wait_for_experiment(
        run_id: str, thread_id: str | None = None
    ) -> dict[str, Any]:
        """Pause only when no useful work remains before this run finishes."""
        return service.wait_for_experiment(run_id, thread_id=thread_id)

    @server.tool()
    def cancel_experiment(run_id: str) -> dict[str, Any]:
        """Cancel a persisted run and write a terminal cancellation event."""
        return service.cancel_experiment(run_id)

    server.run()


if __name__ == "__main__":
    main()

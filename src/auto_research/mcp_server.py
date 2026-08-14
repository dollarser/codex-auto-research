"""Non-interactive experiment lifecycle MCP tools.

Research decisions and Goal state stay in Codex. Durable submission uses the
``auto-research submit`` CLI so it does not depend on an MCP approval prompt.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from .config import load_config
from .ledger import read_json
from .research_session import SESSION_FILE_NAME
from .run_registry import add_active_run
from .runner import ExperimentRunner
from .state_paths import resolve_thread_id, thread_state_root, validate_thread_id
from .supervisor import is_supervisor_thread
from .supervisor_process import ensure_supervisor_running


class ExperimentService:
    def __init__(
        self,
        project_dir: str | Path | None = None,
        thread_id: str | None = None,
        *,
        ensure_supervisor: bool = True,
    ):
        self.project_dir = Path(
            project_dir or os.environ.get("AUTO_RESEARCH_PROJECT_DIR", ".")
        ).resolve()
        self.config = load_config(self.project_dir)
        environment_thread_id = (os.environ.get("CODEX_THREAD_ID") or "").strip()
        self.thread_id = thread_id or environment_thread_id or None
        self._runners: dict[str, ExperimentRunner] = {}
        self.ensure_supervisor = ensure_supervisor

    def _select_thread_id(self, value: str | None = None) -> str:
        if value is not None:
            return resolve_thread_id(value)
        if self.thread_id is not None:
            return validate_thread_id(self.thread_id)
        return resolve_thread_id()

    def _runner(self, thread_id: str) -> ExperimentRunner:
        selected = resolve_thread_id(thread_id, environment={})
        if selected not in self._runners:
            root = thread_state_root(self.project_dir, selected, environment={})
            self._runners[selected] = ExperimentRunner(root / "runs", config=self.config)
        return self._runners[selected]

    @property
    def runner(self) -> ExperimentRunner:
        return self._runner(self._select_thread_id())

    def _worktree(self, value: str) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.project_dir / candidate
        candidate = candidate.resolve()
        if not candidate.is_dir():
            raise ValueError(f"worktree does not exist: {candidate}")
        return candidate

    def _goal_contract_snapshot(self) -> dict[str, Any]:
        path = self.project_dir / "GOAL.md"
        if not path.is_file():
            return {}
        try:
            objective = path.read_text(encoding="utf-8").strip()
            if not objective:
                raise ValueError("GOAL.md must contain a non-empty objective")
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"invalid GOAL.md; repair the goal before starting an experiment: {exc}"
            ) from exc
        return {"digest": hashlib.sha256(objective.encode("utf-8")).hexdigest()}

    def submit_experiment(
        self,
        idea_id: str,
        worktree: str,
        command: str,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        thread_id: str | None = None,
        gpu_ids: list[int] | None = None,
        expected_artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        if not idea_id or not command:
            raise ValueError("idea_id and command are required")
        timeout_s = timeout_s or self.config.default_experiment_timeout_s
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        resolved = self._worktree(worktree)
        snapshot = self._goal_contract_snapshot()
        selected_thread_id = self._select_thread_id(thread_id)
        state_root = thread_state_root(self.project_dir, selected_thread_id)
        runner = self._runner(selected_thread_id)
        supervisor_owned = is_supervisor_thread(self.project_dir, selected_thread_id)
        if not supervisor_owned:
            raise ValueError(
                "submit_experiment requires the project-bound managed Supervisor "
                "Goal task; experiment Workers may only be launched by Supervisor"
            )
        thread_id = selected_thread_id
        session = read_json(state_root / SESSION_FILE_NAME, {}) or {}
        goal_cycle_id = session.get("current_cycle_id")
        if not isinstance(goal_cycle_id, str) or not goal_cycle_id:
            raise ValueError("managed research session has no current Goal cycle")
        run_id = runner.submit(
            idea_id,
            resolved,
            command,
            timeout_s,
            env,
            idempotency_key,
            goal_contract_digest=snapshot.get("digest"),
            codex_thread_id=thread_id,
            launch_worker=False,
            goal_cycle_id=goal_cycle_id,
            resources={"gpu_ids": gpu_ids or []},
            expected_artifacts=expected_artifacts or [],
        )
        add_active_run(state_root, run_id=run_id, thread_id=thread_id)
        supervisor = None
        if self.ensure_supervisor:
            try:
                supervisor = ensure_supervisor_running(
                    self.project_dir, thread_id=thread_id
                )
            except Exception as exc:  # noqa: BLE001 - durable commit already succeeded
                # The run and registry marker are already durable. Returning the
                # run id prevents a retry from accidentally creating a duplicate.
                supervisor = {
                    "status": "REPAIR_PENDING",
                    "error": f"{type(exc).__name__}: {exc}",
                }
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
            "supervisor": supervisor,
        }

    def get_experiment_result(
        self, run_id: str, thread_id: str | None = None
    ) -> dict[str, Any]:
        runner = self._runner(self._select_thread_id(thread_id))
        result = runner.get_result(run_id)
        if result is None:
            run = runner.get_run(run_id)
            return {"run_id": run_id, "status": run.get("status")}
        return result.to_dict()

    def cancel_experiment(
        self, run_id: str, thread_id: str | None = None
    ) -> dict[str, Any]:
        runner = self._runner(self._select_thread_id(thread_id))
        return runner.cancel(run_id).to_dict()


def main() -> None:
    try:
        from fastmcp import FastMCP
    except ImportError as exc:
        raise SystemExit("Install MCP support with: uv sync --extra mcp") from exc

    service = ExperimentService()
    server = FastMCP("auto-research-experiments")

    @server.tool()
    def get_experiment_result(
        run_id: str, thread_id: str | None = None
    ) -> dict[str, Any]:
        """Read a terminal result, or return RUNNING if no terminal event exists."""
        return service.get_experiment_result(run_id, thread_id)

    @server.tool()
    def cancel_experiment(run_id: str, thread_id: str | None = None) -> dict[str, Any]:
        """Cancel a persisted run and write a terminal cancellation event."""
        return service.cancel_experiment(run_id, thread_id)

    server.run()


if __name__ == "__main__":
    main()

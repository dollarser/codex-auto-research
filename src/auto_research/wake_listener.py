"""One-shot bridge from a durable experiment terminal event to a Codex Goal."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .app_server import AppServerClient, AppServerError
from .config import ResearchConfig, load_config
from .ledger import read_json, write_json_atomic
from .runner import ExperimentRunner, _validate_run_id


class GoalBindingError(RuntimeError):
    pass


@contextmanager
def _listener_lock(path: Path):
    path.touch(exist_ok=True)
    with path.open("r+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "a wake listener is already running for this run"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _now() -> float:
    return time.time()


class GoalWakeListener:
    """Wait for one run, reactivate its exact persisted Goal, then exit."""

    def __init__(
        self,
        project_dir: str | Path,
        run_id: str,
        *,
        thread_id: str | None = None,
        config: ResearchConfig | None = None,
        client_factory: Callable[[], AppServerClient] | None = None,
    ):
        self.project_dir = Path(project_dir).resolve()
        self.run_id = _validate_run_id(run_id)
        self.run_dir = self.project_dir / "research" / "runs" / self.run_id
        if not self.run_dir.is_dir():
            raise FileNotFoundError(f"unknown run: {self.run_id}")
        self.config = config or load_config(self.project_dir)
        self.runner = ExperimentRunner(
            self.project_dir / "research" / "runs", self.config
        )
        self.state_path = self.run_dir / "wake.json"
        self.explicit_thread_id = thread_id
        self.client_factory = client_factory or (
            lambda: AppServerClient(self.project_dir, self.config)
        )

    def _state(self) -> dict[str, Any]:
        return read_json(self.state_path, {}) or {}

    def _save(self, **updates: Any) -> dict[str, Any]:
        state = {
            "schema_version": 1,
            "run_id": self.run_id,
            "project_dir": str(self.project_dir),
            **self._state(),
            **updates,
            "updated_at": _now(),
        }
        write_json_atomic(self.state_path, state)
        return state

    @staticmethod
    def _thread_timestamp(thread: dict[str, Any]) -> float | None:
        value = thread.get("updatedAt", thread.get("createdAt"))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # Some protocol builds expose milliseconds, others seconds.
            return float(value) / 1000.0 if value > 10_000_000_000 else float(value)
        return None

    def _discover_thread(self, client: AppServerClient, run_created_at: float) -> str:
        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for thread in client.list_threads():
            thread_id = thread.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                continue
            try:
                goal = client.get_goal(thread_id)
            except AppServerError:
                continue
            if goal and goal.get("status") in {"active", "paused"}:
                candidates.append((thread, goal))
        if not candidates:
            raise GoalBindingError(
                "no active or paused Codex Goal was found for the project cwd; "
                "pass --thread-id or start from a Codex Goal with CODEX_THREAD_ID"
            )

        active = [item for item in candidates if item[1].get("status") == "active"]
        pool = active or candidates
        ranked = sorted(
            pool,
            key=lambda item: self._thread_timestamp(item[0]) or 0.0,
            reverse=True,
        )
        selected_thread = ranked[0][0]
        timestamp = self._thread_timestamp(selected_thread)
        if (
            timestamp is not None
            and timestamp < run_created_at - self.config.bind_recency_s
        ):
            raise GoalBindingError(
                "the newest matching Goal is older than this run; refusing to wake a historical task"
            )
        if len(ranked) > 1:
            first = self._thread_timestamp(ranked[0][0])
            second = self._thread_timestamp(ranked[1][0])
            if first is None or first == second:
                raise GoalBindingError(
                    "multiple equally recent Goals match this project; pass an explicit --thread-id"
                )
        return str(selected_thread["id"])

    def _bind_once(self) -> str:
        state = self._state()
        existing = state.get("thread_id")
        run = self.runner.get_run(self.run_id)
        run_created_at = float(run.get("created_at", _now()))
        persisted = existing if isinstance(existing, str) and existing else None
        environment_thread = os.environ.get("CODEX_THREAD_ID") or None
        candidate = persisted or self.explicit_thread_id or environment_thread
        with self.client_factory() as client:
            client.initialize()
            if candidate:
                goal = client.get_goal(candidate)
                exact_binding = bool(persisted or self.explicit_thread_id)
                valid_statuses = {
                    "active",
                    "paused",
                    "complete",
                    "blocked",
                    "usageLimited",
                    "budgetLimited",
                }
                if exact_binding:
                    if not goal or goal.get("status") not in valid_statuses:
                        raise GoalBindingError(
                            f"thread {candidate} has no usable persisted Goal"
                        )
                    thread_id = candidate
                    binding_source = "persisted" if persisted else "explicit"
                else:
                    # CODEX_THREAD_ID is an opportunistic local environment
                    # hint, not part of the public App Server contract. Verify
                    # it still belongs to this cwd and this run's time window;
                    # otherwise use the protocol-backed discovery fallback.
                    listed = next(
                        (
                            item
                            for item in client.list_threads()
                            if item.get("id") == candidate
                        ),
                        None,
                    )
                    timestamp = self._thread_timestamp(listed) if listed else None
                    if (
                        goal
                        and goal.get("status") in {"active", "paused"}
                        and listed
                        and (
                            timestamp is None
                            or timestamp >= run_created_at - self.config.bind_recency_s
                        )
                    ):
                        thread_id = candidate
                        binding_source = "CODEX_THREAD_ID"
                    else:
                        thread_id = self._discover_thread(client, run_created_at)
                        binding_source = "cwd-discovery"
            else:
                thread_id = self._discover_thread(client, run_created_at)
                binding_source = "cwd-discovery"
        self._save(
            state="WAITING",
            thread_id=thread_id,
            binding_source=binding_source,
            bound_at=_now(),
            last_error=None,
        )
        return thread_id

    def _retry(self, phase: str, operation: Callable[[], Any]) -> Any:
        delay = self.config.reconnect_initial_s
        while True:
            try:
                return operation()
            except (AppServerError, GoalBindingError, OSError, RuntimeError) as exc:
                state = self._state()
                attempts = int(state.get("attempts", 0)) + 1
                self._save(
                    state=phase,
                    attempts=attempts,
                    last_error=f"{type(exc).__name__}: {exc}",
                    next_retry_at=_now() + delay,
                )
                time.sleep(delay)
                delay = min(self.config.reconnect_max_s, delay * 2)

    def _activate_goal_once(
        self, thread_id: str, terminal_status: str
    ) -> dict[str, Any]:
        """Reactivate the native Goal scheduler without owning a Codex turn.

        The desktop client may already own the thread writer.  A second App
        Server can still update persisted Goal state, but `thread/resume` or
        `turn/start` would race that writer and can create duplicate execution
        hosts.  The Goal scheduler is the sole owner of continuation turns.
        """
        with self.client_factory() as client:
            client.initialize()
            goal = client.get_goal(thread_id)
            if not goal:
                raise GoalBindingError(f"thread {thread_id} no longer has a Goal")
            goal_status = goal.get("status")
            if goal_status in {
                "complete",
                "blocked",
                "usageLimited",
                "budgetLimited",
            }:
                return self._save(
                    state="SKIPPED",
                    reason=f"Goal is {goal_status}; listener will not reactivate it",
                    terminal_status=terminal_status,
                    finished_at=_now(),
                )

            if goal_status == "active":
                return self._save(
                    state="SKIPPED",
                    reason="Goal is already active; continuation is already owned elsewhere",
                    terminal_status=terminal_status,
                    finished_at=_now(),
                )

            self._save(
                state="ACTIVATING", terminal_status=terminal_status, last_error=None
            )
            activated = client.set_goal_status(thread_id, "active")
            return self._save(
                state="ACTIVATED",
                goal_status=activated.get("status"),
                activated_at=_now(),
                finished_at=_now(),
                last_error=None,
            )

    @staticmethod
    def _goal_usage(goal: dict[str, Any]) -> tuple[int | None, float | None]:
        tokens = goal.get("tokensUsed")
        updated = goal.get("updatedAt")
        return (
            int(tokens)
            if isinstance(tokens, (int, float)) and not isinstance(tokens, bool)
            else None,
            float(updated)
            if isinstance(updated, (int, float)) and not isinstance(updated, bool)
            else None,
        )

    def _pause_goal_after_current_turn(self, thread_id: str) -> dict[str, Any]:
        """Hold the Goal paused across the turn-finalization boundary.

        Setting a Goal to paused does not interrupt the turn that submitted the
        experiment.  That turn may publish updated Goal usage/state when it
        finishes, so the listener waits for that durable boundary and then
        reasserts paused before it begins the long experiment wait.
        """
        run = self.runner.get_run(self.run_id)
        if not run.get("pause_goal_on_turn_end"):
            return self._save(state="WAITING", pause_mode="not_requested")

        with self.client_factory() as client:
            client.initialize()
            goal = client.get_goal(thread_id)
            if not goal:
                raise GoalBindingError(f"thread {thread_id} no longer has a Goal")
            if goal.get("status") in {
                "complete",
                "blocked",
                "usageLimited",
                "budgetLimited",
            }:
                return self._save(
                    state="SKIPPED",
                    reason=f"Goal is {goal.get('status')}; listener will not manage it",
                    finished_at=_now(),
                )

            state = self._state()
            original_tokens, original_updated = self._goal_usage(goal)
            persisted_tokens = state.get("pause_baseline_tokens")
            persisted_updated = state.get("pause_baseline_updated_at")
            baseline_tokens = (
                int(persisted_tokens)
                if isinstance(persisted_tokens, (int, float))
                and not isinstance(persisted_tokens, bool)
                else original_tokens
            )
            baseline_updated = (
                float(persisted_updated)
                if isinstance(persisted_updated, (int, float))
                and not isinstance(persisted_updated, bool)
                else original_updated
            )
            if state.get("pause_boundary_detected_at"):
                confirmed = client.set_goal_status(thread_id, "paused")
                return self._save(
                    state="WAITING",
                    pause_mode="turn_boundary_recovered",
                    pause_boundary_observed_at=state["pause_boundary_detected_at"],
                    pause_confirmed_status=confirmed.get("status"),
                    last_error=None,
                )

            already_advanced = (
                baseline_tokens is not None
                and original_tokens is not None
                and original_tokens > baseline_tokens
            )
            already_overwritten = (
                state.get("state") in {"PAUSE_HANDOFF", "PAUSE_RETRY"}
                and goal.get("status") != "paused"
            )
            if already_advanced or already_overwritten:
                detected_at = _now()
                self._save(
                    state="PAUSE_BOUNDARY_DETECTED",
                    pause_boundary_detected_at=detected_at,
                    pause_boundary_reason=(
                        "usage_advanced" if already_advanced else "status_overwritten"
                    ),
                )
                confirmed = client.set_goal_status(thread_id, "paused")
                return self._save(
                    state="WAITING",
                    pause_mode="turn_boundary_recovered",
                    pause_boundary_observed_at=detected_at,
                    pause_confirmed_status=confirmed.get("status"),
                    last_error=None,
                )

            paused = client.set_goal_status(thread_id, "paused")
            paused_tokens, paused_updated = self._goal_usage(paused)
            baseline_tokens = (
                baseline_tokens if baseline_tokens is not None else paused_tokens
            )
            baseline_updated = (
                baseline_updated if baseline_updated is not None else paused_updated
            )
            self._save(
                state="PAUSE_HANDOFF",
                pause_requested_at=_now(),
                pause_baseline_tokens=baseline_tokens,
                pause_baseline_updated_at=baseline_updated,
                last_error=None,
            )

            while True:
                time.sleep(max(self.config.event_poll_s, 0.1))
                current = client.get_goal(thread_id)
                if not current:
                    raise GoalBindingError(
                        f"thread {thread_id} lost its Goal during pause handoff"
                    )
                status = current.get("status")
                if status in {
                    "complete",
                    "blocked",
                    "usageLimited",
                    "budgetLimited",
                }:
                    return self._save(
                        state="SKIPPED",
                        reason=f"Goal became {status} during pause handoff",
                        finished_at=_now(),
                    )
                tokens, updated = self._goal_usage(current)
                usage_advanced = (
                    baseline_tokens is not None
                    and tokens is not None
                    and tokens > baseline_tokens
                )
                state_overwritten = status != "paused"
                if usage_advanced or state_overwritten:
                    detected_at = _now()
                    reason = (
                        "usage_advanced" if usage_advanced else "status_overwritten"
                    )
                    self._save(
                        state="PAUSE_BOUNDARY_DETECTED",
                        pause_boundary_detected_at=detected_at,
                        pause_boundary_reason=reason,
                    )
                    confirmed = client.set_goal_status(thread_id, "paused")
                    return self._save(
                        state="WAITING",
                        pause_mode="turn_boundary",
                        pause_boundary_observed_at=detected_at,
                        pause_boundary_reason=reason,
                        pause_confirmed_status=confirmed.get("status"),
                        pause_confirmed_tokens=tokens,
                        pause_confirmed_updated_at=updated,
                        last_error=None,
                    )

    def run(self) -> dict[str, Any]:
        with _listener_lock(self.run_dir / ".wake-listener.lock"):
            state = self._state()
            if state.get("state") in {"ACTIVATED", "SKIPPED"}:
                return state
            self._save(state="BINDING", listener_pid=os.getpid(), started_at=_now())
            thread_id = self._retry("BINDING_RETRY", self._bind_once)
            pause_state = self._retry(
                "PAUSE_RETRY",
                lambda: self._pause_goal_after_current_turn(thread_id),
            )
            if pause_state.get("state") == "SKIPPED":
                return pause_state
            result = self.runner.wait(
                self.run_id,
                poll_s=self.config.event_poll_s,
                grace_s=self.config.event_grace_s,
            )
            self._save(
                state="TERMINAL",
                terminal_status=result.status,
                event_path=result.event_path,
                terminal_at=_now(),
            )
            return self._retry(
                "ACTIVATION_RETRY",
                lambda: self._activate_goal_once(thread_id, result.status),
            )


def spawn_wake_listener(
    project_dir: str | Path,
    run_id: str,
    *,
    thread_id: str | None = None,
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    run_id = _validate_run_id(run_id)
    run_dir = project / "research" / "runs" / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError(f"unknown run: {run_id}")
    command = [
        sys.executable,
        "-m",
        "auto_research.wake_listener",
        "--project",
        str(project),
        "--run-id",
        run_id,
    ]
    if thread_id:
        command.extend(["--thread-id", thread_id])
    log_path = run_dir / "wake-listener.log"
    stream = log_path.open("ab")
    process = subprocess.Popen(
        command,
        cwd=str(project),
        stdin=subprocess.DEVNULL,
        stdout=stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    stream.close()
    threading.Thread(target=process.wait, daemon=True).start()
    write_json_atomic(
        run_dir / "wake-launch.json",
        {
            "run_id": run_id,
            "thread_id": thread_id,
            "listener_pid": process.pid,
            "command": command,
            "launched_at": _now(),
        },
    )
    return {"status": "ARMED", "pid": process.pid, "thread_id": thread_id}


def recover_wake_listeners(project_dir: str | Path) -> list[dict[str, Any]]:
    project = Path(project_dir).resolve()
    recovered: list[dict[str, Any]] = []
    for run_file in sorted(project.joinpath("research", "runs").glob("run-*/run.json")):
        run = read_json(run_file, {}) or {}
        run_id = run.get("run_id")
        if not isinstance(run_id, str):
            continue
        if run.get("runtime_version") != 3 or run.get("wake_enabled") is not True:
            # Never attach the v0.3 listener to pre-v0.3 historical runs.
            continue
        wake = read_json(run_file.parent / "wake.json", {}) or {}
        if wake.get("state") in {"ACTIVATED", "SKIPPED"}:
            continue
        thread_id = wake.get("thread_id")
        recovered.append(
            {
                "run_id": run_id,
                **spawn_wake_listener(
                    project,
                    run_id,
                    thread_id=thread_id if isinstance(thread_id, str) else None,
                ),
            }
        )
    return recovered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auto-research-wake-listener")
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--thread-id")
    args = parser.parse_args(argv)
    result = GoalWakeListener(
        args.project,
        args.run_id,
        thread_id=args.thread_id,
    ).run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

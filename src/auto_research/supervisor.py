"""Single-writer App Server scheduler for autonomous research Turns."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .app_server import AppServerClient
from .ledger import read_json, write_json_atomic
from .research_session import ResearchSessionManager
from .runner import ExperimentRunner

HANDOFF_ACTIONS = {
    "WAIT_FOR_RUN",
    "CONTINUE_NOW",
    "NEEDS_USER",
    "COMPLETE",
    "FAILED_STOP",
}
SUPERVISOR_TERMINAL_STATES = {
    "NEEDS_USER",
    "COMPLETED",
    "FAILED_STOP",
    "HANDOFF_UNCONFIRMED",
    "RECOVERY_ERROR",
}
SUPERVISOR_RESUMABLE_STATES = {
    "NEEDS_USER",
    "HANDOFF_UNCONFIRMED",
    "RECOVERY_ERROR",
}
STATE_SCHEMA_VERSION = 1


class SupervisorError(RuntimeError):
    pass


def supervisor_dir(project_dir: str | Path) -> Path:
    return Path(project_dir).resolve() / "research" / "supervisor"


def read_supervisor_state(project_dir: str | Path) -> dict[str, Any] | None:
    value = read_json(supervisor_dir(project_dir) / "state.json", None)
    return value if isinstance(value, dict) else None


def is_supervisor_thread(project_dir: str | Path, thread_id: str | None) -> bool:
    state = read_supervisor_state(project_dir)
    return bool(
        thread_id
        and state
        and state.get("thread_id") == thread_id
        and state.get("state") not in {"COMPLETED", "FAILED_STOP"}
    )


def submit_handoff(
    project_dir: str | Path,
    *,
    turn_attempt_id: str,
    action: str,
    run_id: str | None = None,
    summary: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Persist the exact structured decision consumed by the Supervisor."""
    project = Path(project_dir).resolve()
    if action not in HANDOFF_ACTIONS:
        raise ValueError(f"action must be one of {sorted(HANDOFF_ACTIONS)}")
    active = read_json(supervisor_dir(project) / "active_turn.json", {}) or {}
    if active.get("turn_attempt_id") != turn_attempt_id:
        raise ValueError("turn_attempt_id does not match the active Supervisor Turn")
    if action == "WAIT_FOR_RUN" and not run_id:
        raise ValueError("WAIT_FOR_RUN requires run_id")
    if run_id:
        runner = ExperimentRunner(project / "research" / "runs")
        run = runner.get_run(run_id)
        if run.get("codex_thread_id") != active.get("thread_id"):
            raise ValueError("run_id is not owned by the active Supervisor thread")
    record = {
        "schema_version": STATE_SCHEMA_VERSION,
        "turn_attempt_id": turn_attempt_id,
        "thread_id": active.get("thread_id"),
        "turn_id": active.get("turn_id"),
        "action": action,
        "run_id": run_id,
        "summary": summary.strip(),
        "reason": reason.strip(),
        "submitted_at": time.time(),
    }
    path = supervisor_dir(project) / "handoffs" / f"{turn_attempt_id}.json"
    if path.exists():
        existing = read_json(path, {}) or {}
        comparable = {k: existing.get(k) for k in record if k != "submitted_at"}
        expected = {k: value for k, value in record.items() if k != "submitted_at"}
        if comparable != expected:
            raise ValueError("a different handoff already exists for this Turn")
        return existing
    write_json_atomic(path, record)
    return record


class AppServerSupervisor:
    """Drive exactly one dedicated research Thread from durable handoffs."""

    def __init__(
        self,
        project_dir: str | Path,
        *,
        client_factory: Callable[[], Any] | None = None,
        session_factory: Callable[[], Any] | None = None,
        runner: ExperimentRunner | None = None,
    ):
        self.project_dir = Path(project_dir).resolve()
        self.control_dir = supervisor_dir(self.project_dir)
        self.state_path = self.control_dir / "state.json"
        self.active_turn_path = self.control_dir / "active_turn.json"
        self.lock_path = self.control_dir / "scheduler.lock"
        self.client_factory = client_factory or (
            lambda: AppServerClient(
                self.project_dir,
                client_name="auto-research-supervisor",
                client_version="0.4.0",
            )
        )
        self.session_factory = session_factory or (
            lambda: ResearchSessionManager(self.project_dir)
        )
        self.runner = runner or ExperimentRunner(self.project_dir / "research" / "runs")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.control_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        import fcntl

        with self.lock_path.open("r+") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SupervisorError("another Supervisor owns this project") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _write_state(self, **updates: Any) -> dict[str, Any]:
        state = read_json(self.state_path, {}) or {}
        state.update(updates)
        state.update(
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "project_root": str(self.project_dir),
                "updated_at": time.time(),
            }
        )
        write_json_atomic(self.state_path, state)
        return state

    def _prepare_thread(self) -> dict[str, Any]:
        return self.session_factory().prepare(create_thread=True)

    @staticmethod
    def _prompt(attempt_id: str, terminal_result: dict[str, Any] | None) -> str:
        evidence = (
            "No previous experiment terminal event is pending."
            if terminal_result is None
            else "The previous experiment reached a durable terminal state:\n"
            + json.dumps(terminal_result, ensure_ascii=False, indent=2)
        )
        return f"""Continue the autonomous research goal for this dedicated task.

Supervisor turn_attempt_id: {attempt_id}

{evidence}

Inspect the real repository and durable experiment artifacts, decide the next
research action, and perform the useful work. Before ending this Turn, you MUST
call the experiment MCP tool `submit_supervisor_handoff` exactly once with this
turn_attempt_id and one action:
- WAIT_FOR_RUN with the detached run_id after startup has been validated;
- CONTINUE_NOW only when another immediate Turn is genuinely required;
- NEEDS_USER when authorization or a user decision is required;
- COMPLETE only when the overall research goal is achieved with evidence;
- FAILED_STOP when no safe recovery exists.

Do not use Goal active/paused/blocked as a wake mechanism. The Supervisor owns
Turn scheduling and waits without model polling while an experiment runs.
"""

    def _route_handoff(self, handoff: dict[str, Any]) -> dict[str, Any]:
        action = handoff.get("action")
        if action == "WAIT_FOR_RUN":
            run_id = str(handoff["run_id"])
            run = self.runner.get_run(run_id)
            state = read_json(self.state_path, {}) or {}
            if run.get("codex_thread_id") != state.get("thread_id"):
                raise SupervisorError("handoff run is owned by another thread")
            return self._write_state(
                state="EXPERIMENT_WAITING",
                waiting_run_id=run_id,
                last_handoff=handoff,
                immediate_continuations=0,
            )
        if action == "CONTINUE_NOW":
            state = read_json(self.state_path, {}) or {}
            count = int(state.get("immediate_continuations", 0)) + 1
            if count > 1:
                return self._write_state(
                    state="HANDOFF_UNCONFIRMED",
                    error="CONTINUE_NOW limit exceeded; operator review required",
                    last_handoff=handoff,
                )
            return self._write_state(
                state="TURN_READY",
                waiting_run_id=None,
                last_handoff=handoff,
                immediate_continuations=count,
            )
        target = {
            "NEEDS_USER": "NEEDS_USER",
            "COMPLETE": "COMPLETED",
            "FAILED_STOP": "FAILED_STOP",
        }.get(action)
        if target is None:
            raise SupervisorError(f"unsupported handoff action: {action!r}")
        return self._write_state(
            state=target,
            waiting_run_id=None,
            last_handoff=handoff,
        )

    def run(self, *, max_turns: int | None = None) -> dict[str, Any]:
        """Run until a terminal operator state, optionally bounded for testing."""
        with self._lock():
            session = self._prepare_thread()
            thread_id = str(session["thread_id"])
            state = read_json(self.state_path, {}) or {}
            if state and state.get("thread_id") not in {None, thread_id}:
                raise SupervisorError("Supervisor state is bound to another thread")
            current = str(state.get("state") or "TURN_READY")
            if current in SUPERVISOR_TERMINAL_STATES:
                return self._write_state(thread_id=thread_id, state=current)
            self._write_state(thread_id=thread_id, state=current)
            turns_started = 0
            with self.client_factory() as client:
                client.initialize()
                client.resume_thread(thread_id)
                while True:
                    state = read_json(self.state_path, {}) or {}
                    current = str(state.get("state") or "TURN_READY")
                    if current in SUPERVISOR_TERMINAL_STATES:
                        return state
                    if current == "EXPERIMENT_WAITING":
                        run_id = state.get("waiting_run_id")
                        if not isinstance(run_id, str):
                            return self._write_state(
                                state="RECOVERY_ERROR",
                                error="EXPERIMENT_WAITING has no run_id",
                            )
                        result = self.runner.wait(run_id).to_dict()
                        self._write_state(
                            state="TURN_READY",
                            waiting_run_id=None,
                            pending_terminal_result=result,
                        )
                        continue
                    if current != "TURN_READY":
                        return self._write_state(
                            state="RECOVERY_ERROR",
                            error=f"cannot safely recover state {current}",
                        )
                    if max_turns is not None and turns_started >= max_turns:
                        return state
                    attempt_id = f"attempt-{uuid.uuid4().hex}"
                    active = {
                        "schema_version": STATE_SCHEMA_VERSION,
                        "turn_attempt_id": attempt_id,
                        "thread_id": thread_id,
                        "turn_id": None,
                        "created_at": time.time(),
                    }
                    write_json_atomic(self.active_turn_path, active)
                    self._write_state(
                        state="TURN_STARTING",
                        active_turn_attempt_id=attempt_id,
                    )
                    turn = client.start_turn(
                        thread_id,
                        self._prompt(attempt_id, state.get("pending_terminal_result")),
                    )
                    active["turn_id"] = turn["id"]
                    write_json_atomic(self.active_turn_path, active)
                    self._write_state(
                        state="TURN_RUNNING",
                        active_turn_id=turn["id"],
                        pending_terminal_result=None,
                    )
                    completed = client.wait_turn(thread_id, str(turn["id"]))
                    turns_started += 1
                    self._write_state(
                        state="HANDOFF_RECONCILING",
                        last_turn=completed,
                    )
                    handoff_path = self.control_dir / "handoffs" / f"{attempt_id}.json"
                    handoff = read_json(handoff_path, None)
                    if not isinstance(handoff, dict):
                        return self._write_state(
                            state="HANDOFF_UNCONFIRMED",
                            error="Turn completed without a structured handoff",
                        )
                    self.active_turn_path.unlink(missing_ok=True)
                    self._route_handoff(handoff)

    def resume(self) -> dict[str, Any]:
        state = read_json(self.state_path, {}) or {}
        if state.get("state") not in SUPERVISOR_RESUMABLE_STATES:
            raise SupervisorError("Supervisor is not in an operator-paused state")
        return self._write_state(
            state="TURN_READY",
            error=None,
            waiting_run_id=None,
            immediate_continuations=0,
        )


def spawn_supervisor(project_dir: str | Path) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    control = supervisor_dir(project)
    control.mkdir(parents=True, exist_ok=True)
    log = (control / "supervisor.log").open("ab")
    process = subprocess.Popen(
        [sys.executable, "-m", "auto_research.supervisor", "--project", str(project)],
        cwd=project,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),
    )
    log.close()
    write_json_atomic(
        control / "process.json",
        {"pid": process.pid, "started_at": time.time(), "project_root": str(project)},
    )
    return {
        "status": "STARTED",
        "pid": process.pid,
        "log": str(control / "supervisor.log"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m auto_research.supervisor")
    parser.add_argument("--project", default=".")
    args = parser.parse_args(argv)
    state = AppServerSupervisor(args.project).run()
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0 if state.get("state") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())

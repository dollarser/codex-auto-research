from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .command_policy import command_to_argv
from .config import ResearchConfig, load_config
from .ledger import read_json, write_json_atomic
from .models import TerminalRunResult

TERMINAL_EVENT_NAMES = (
    "completed.json",
    "failed.json",
    "timeout.json",
    "cancelled.json",
    "lost.json",
)
RUN_ID_PATTERN = re.compile(r"^run-[A-Za-z0-9._-]+$")


def _safe_id(value: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in "._-" else "-" for char in value
    )
    return cleaned.strip(".-")[:120] or "idea"


def _validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return run_id


@contextmanager
def _terminal_lock(run_dir: Path):
    lock_path = run_dir / ".terminal.lock"
    lock_path.touch(exist_ok=True)
    try:
        import fcntl

        with lock_path.open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except ImportError:
        yield


def finalize_run(
    run_dir: Path, event_name: str, event: dict[str, Any], run: dict[str, Any]
) -> bool:
    """Commit exactly one terminal event and return whether this call won."""
    with _terminal_lock(run_dir):
        if any((run_dir / "events" / name).exists() for name in TERMINAL_EVENT_NAMES):
            return False
        write_json_atomic(
            run_dir / "run.json",
            {**run, "status": event["status"], "return_code": event.get("return_code")},
        )
        write_json_atomic(run_dir / "events" / event_name, event)
        return True


def _run_id(idea_id: str) -> str:
    return f"run-{_safe_id(idea_id)}-{uuid.uuid4().hex[:8]}"


class ExperimentRunner:
    """Durable detached worker launcher and terminal-result waiter."""

    def __init__(self, runs_dir: str | Path, config: ResearchConfig | None = None):
        self.runs_dir = Path(runs_dir)
        self.config = config
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def submit(
        self,
        idea_id: str,
        worktree: str | Path,
        command: str | list[str] | tuple[str, ...],
        timeout_s: int,
        env: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        goal_contract_digest: str | None = None,
        goal_contract_revision: int | None = None,
        hard_requirements_snapshot: list[dict[str, Any]] | None = None,
        wake_enabled: bool = False,
        codex_thread_id: str | None = None,
        pause_goal_on_turn_end: bool = False,
    ) -> str:
        with _terminal_lock(self.runs_dir):
            if idempotency_key:
                for existing_dir in self.runs_dir.glob("run-*"):
                    existing = read_json(existing_dir / "run.json", {})
                    if existing.get("idempotency_key") == idempotency_key:
                        return existing["run_id"]
            config = self.config or load_config(self.runs_dir.parent.parent)
            use_shell = config.use_shell
            argv = (
                command_to_argv(command, config.allowed_executables)
                if not use_shell
                else shlex.split(command)
                if isinstance(command, str)
                else list(command)
            )
            run_id = _run_id(idea_id)
            run_dir = self.runs_dir / run_id
            events_dir = run_dir / "events"
            events_dir.mkdir(parents=True)
            run = {
                "run_id": run_id,
                "idea_id": idea_id,
                "worktree": str(Path(worktree).resolve()),
                "command": command
                if isinstance(command, str)
                else " ".join(shlex.quote(part) for part in argv),
                "argv": argv,
                "shell": use_shell,
                "timeout_s": timeout_s,
                "env": env or {},
                "idempotency_key": idempotency_key,
                "goal_contract_digest": goal_contract_digest,
                "goal_contract_revision": goal_contract_revision,
                "hard_requirements_snapshot": hard_requirements_snapshot or [],
                "runtime_version": 3,
                "wake_enabled": wake_enabled,
                "codex_thread_id": codex_thread_id,
                "pause_goal_on_turn_end": pause_goal_on_turn_end,
                "created_at": time.time(),
                "status": "SUBMITTED",
                "worker_heartbeat_s": config.worker_heartbeat_s,
            }
            write_json_atomic(run_dir / "run.json", run)
            worker_log = (run_dir / "worker.log").open("ab")
            worker_cmd = [
                sys.executable,
                "-m",
                "auto_research.runner_worker",
                "--run-dir",
                str(run_dir),
            ]
            process_env = os.environ.copy()
            process_env.update(env or {})
            # The Harness/MCP process may itself be supervised by launchd with
            # a minimal PATH.  Put the interpreter environment that launched
            # this runner first so `python`/`python3` in an experiment resolve
            # to the same environment as the Worker, including its packages.
            interpreter_bin = str(Path(sys.executable).parent)
            current_path = process_env.get("PATH", "")
            process_env["PATH"] = interpreter_bin + (
                os.pathsep + current_path if current_path else ""
            )
            process_env["AUTO_RESEARCH_RUN_ID"] = run_id
            process_env["AUTO_RESEARCH_RUN_DIR"] = str(run_dir)
            process = subprocess.Popen(
                worker_cmd,
                cwd=str(Path(worktree).resolve()),
                stdin=subprocess.DEVNULL,
                stdout=worker_log,
                stderr=subprocess.STDOUT,
                env=process_env,
                start_new_session=True,
                close_fds=True,
            )
            worker_log.close()
            # Reap the detached supervisor when it exits so short-lived callers do
            # not emit ResourceWarning while the durable worker keeps running.
            threading.Thread(target=process.wait, daemon=True).start()
            run["worker_pid"] = process.pid
            run["status"] = "RUNNING"
            write_json_atomic(run_dir / "run.json", run)
            return run_id

    def annotate_run(self, run_id: str, **metadata: Any) -> None:
        """Add controller provenance to a durable run without changing status."""
        run_id = _validate_run_id(run_id)
        run_dir = self.runs_dir / run_id
        if not run_dir.exists():
            raise FileNotFoundError(f"Unknown run: {run_id}")
        with _terminal_lock(run_dir):
            run = read_json(run_dir / "run.json", {})
            if not run:
                raise FileNotFoundError(f"Missing run metadata: {run_id}")
            write_json_atomic(run_dir / "run.json", {**run, **metadata})

    def wait(
        self, run_id: str, poll_s: float | None = None, grace_s: float | None = None
    ) -> TerminalRunResult:
        """Wait locally on a terminal event; no model or network polling occurs.

        Prefer the optional OS-backed watcher when installed. The polling
        fallback is still entirely local and never creates an LLM/MCP status
        request.
        """
        run_id = _validate_run_id(run_id)
        if poll_s is None:
            poll_s = self.config.event_poll_s if self.config else 0.25
        if grace_s is None:
            grace_s = self.config.event_grace_s if self.config else 30.0
        run_dir = self.runs_dir / run_id
        if not run_dir.exists():
            raise FileNotFoundError(f"Unknown run: {run_id}")
        existing = self.get_result(run_id)
        if existing:
            return existing
        run = read_json(run_dir / "run.json", {})
        deadline = (
            float(run.get("created_at", time.time()))
            + int(run.get("timeout_s", 3600))
            + grace_s
        )

        def terminal_or_lost() -> TerminalRunResult | None:
            result = self.get_result(run_id)
            if result:
                return result
            if time.time() >= deadline:
                return self._mark_lost(run_dir, run)
            return None

        try:
            from watchfiles import watch
        except ImportError:
            watch = None
        if watch is not None:
            # `yield_on_timeout` closes the race where the terminal file is
            # created after the preflight check but before the OS watcher is
            # fully registered. Even without a later file event, the local
            # timeout tick rechecks durable state and the LOST deadline.
            for _ in watch(
                str(run_dir / "events"),
                debounce=250,
                step=250,
                rust_timeout=1000,
                yield_on_timeout=True,
            ):
                result = terminal_or_lost()
                if result:
                    return result
            raise RuntimeError(f"Event watcher stopped unexpectedly: {run_id}")
        while True:
            result = terminal_or_lost()
            if result:
                return result
            time.sleep(poll_s)

    def get_result(self, run_id: str) -> TerminalRunResult | None:
        run_id = _validate_run_id(run_id)
        run_dir = self.runs_dir / run_id
        if not run_dir.exists():
            raise FileNotFoundError(f"Unknown run: {run_id}")
        for name in TERMINAL_EVENT_NAMES:
            event_path = run_dir / "events" / name
            if event_path.exists():
                return self._result_from_event(run_dir, event_path)
        return None

    def get_run(self, run_id: str) -> dict[str, Any]:
        run_id = _validate_run_id(run_id)
        run = read_json(self.runs_dir / run_id / "run.json", {}) or {}
        if not run:
            raise FileNotFoundError(f"Unknown run: {run_id}")
        return run

    def cancel(self, run_id: str) -> TerminalRunResult:
        run_id = _validate_run_id(run_id)
        run_dir = self.runs_dir / run_id
        run = read_json(run_dir / "run.json", {})
        if not run:
            raise FileNotFoundError(f"Unknown run: {run_id}")
        with _terminal_lock(run_dir):
            existing = self.get_result(run_id)
            if existing:
                return existing
            # The worker holds this same lock while checking the cancellation
            # state and recording child_pid. This closes the startup race where
            # cancellation could happen before the real experiment was spawned.
            write_json_atomic(
                run_dir / "cancel.requested.json",
                {
                    "run_id": run_id,
                    "requested_at": time.time(),
                },
            )
            run = read_json(run_dir / "run.json", run)
            for key in ("child_pid", "worker_pid"):
                pid = run.get(key)
                if not pid:
                    continue
                try:
                    os.killpg(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            event = {
                "event": "RUN_CANCELLED",
                "run_id": run_id,
                "idea_id": run.get("idea_id", ""),
                "status": "CANCELLED",
                "error": "cancelled by operator",
                "finished_at": time.time(),
            }
            write_json_atomic(run_dir / "run.json", {**run, "status": "CANCELLED"})
            write_json_atomic(run_dir / "events" / "cancelled.json", event)
        result = self.get_result(run_id)
        if result is None:
            raise RuntimeError(f"Could not commit terminal state for {run_id}")
        return result

    def _result_from_event(self, run_dir: Path, event_path: Path) -> TerminalRunResult:
        event = read_json(event_path, {})
        metrics = read_json(run_dir / "metrics.json", {}) or {}
        run = read_json(run_dir / "run.json", {}) or {}

        def tail(name: str, limit: int = 12000) -> str:
            path = run_dir / name
            if not path.exists():
                return ""
            try:
                data = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
            return data[-limit:]

        return TerminalRunResult(
            run_id=event.get("run_id", run_dir.name),
            idea_id=event.get("idea_id", ""),
            status=event.get("status", "FAILED"),
            return_code=event.get("return_code"),
            metrics=metrics,
            error=event.get("error", ""),
            result_dir=str(run_dir),
            event_path=str(event_path),
            stdout_tail=tail("stdout.log"),
            stderr_tail=tail("stderr.log"),
            command=str(run.get("command", "")),
            argv=list(run.get("argv", [])),
            worktree=str(run.get("worktree", "")),
        )

    def _mark_lost(self, run_dir: Path, run: dict[str, Any]) -> TerminalRunResult:
        existing = self.get_result(run["run_id"])
        if existing:
            return existing
        for key in ("child_pid", "worker_pid"):
            pid = run.get(key)
            if not pid:
                continue
            try:
                os.killpg(int(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        event = {
            "event": "RUN_LOST",
            "run_id": run["run_id"],
            "idea_id": run.get("idea_id", ""),
            "status": "LOST",
            "error": "no terminal event before watchdog deadline",
            "finished_at": time.time(),
        }
        finalize_run(run_dir, "lost.json", event, run)
        result = self.get_result(run["run_id"])
        if result is None:
            raise RuntimeError(f"Could not commit terminal state for {run['run_id']}")
        return result


def parse_command(command: str) -> list[str]:
    """Expose a safe helper for callers that want argv rather than shell text."""
    return command_to_argv(command)

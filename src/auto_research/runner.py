from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import ResearchConfig, load_config
from .ledger import read_json, write_json_atomic
from .models import TerminalRunResult
from .process_identity import (
    process_identity_state,
    process_start_ticks,
    terminate_process_group,
)

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
        current = read_json(run_dir / "run.json", {}) or run
        write_json_atomic(
            run_dir / "run.json",
            {
                **current,
                "status": event["status"],
                "return_code": event.get("return_code"),
            },
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

    @staticmethod
    def _update_run(run_dir: Path, **updates: Any) -> dict[str, Any]:
        """Patch current metadata under the same lock used by Worker/cancel."""
        with _terminal_lock(run_dir):
            current = read_json(run_dir / "run.json", {}) or {}
            if not current:
                raise FileNotFoundError(f"Missing run metadata: {run_dir.name}")
            current.update(updates)
            write_json_atomic(run_dir / "run.json", current)
            return current

    def submit(
        self,
        idea_id: str,
        worktree: str | Path,
        command: str | list[str] | tuple[str, ...],
        timeout_s: int,
        env: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        goal_contract_digest: str | None = None,
        codex_thread_id: str | None = None,
        launch_worker: bool = True,
        goal_cycle_id: str | None = None,
        resources: dict[str, Any] | None = None,
        expected_artifacts: list[str] | None = None,
    ) -> str:
        request_digest = hashlib.sha256(
            json.dumps(
                {
                    "idea_id": idea_id,
                    "worktree": str(Path(worktree).resolve()),
                    "command": command,
                    "timeout_s": timeout_s,
                    "env": env or {},
                    "goal_cycle_id": goal_cycle_id,
                    "resources": resources or {},
                    "expected_artifacts": expected_artifacts or [],
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        with _terminal_lock(self.runs_dir):
            if idempotency_key:
                for existing_dir in self.runs_dir.glob("run-*"):
                    existing = read_json(existing_dir / "run.json", {})
                    if existing.get("idempotency_key") == idempotency_key:
                        if existing.get("request_digest") != request_digest:
                            raise ValueError(
                                "idempotency_key already belongs to a different request"
                            )
                        return existing["run_id"]
            config = self.config or load_config(self.runs_dir.parent.parent)
            argv = shlex.split(command) if isinstance(command, str) else list(command)
            run_id = _run_id(idea_id)
            run_dir = self.runs_dir / run_id
            events_dir = run_dir / "events"
            events_dir.mkdir(parents=True)
            run_env = dict(env or {})
            run = {
                "run_id": run_id,
                "idea_id": idea_id,
                "worktree": str(Path(worktree).resolve()),
                "command": command
                if isinstance(command, str)
                else " ".join(shlex.quote(part) for part in argv),
                "argv": argv,
                "shell": True,
                "timeout_s": timeout_s,
                "env_keys": sorted(run_env),
                "idempotency_key": idempotency_key,
                "request_digest": request_digest,
                "goal_cycle_id": goal_cycle_id,
                "resources": resources or {},
                "expected_artifacts": expected_artifacts or [],
                "goal_contract_digest": goal_contract_digest,
                "codex_thread_id": codex_thread_id,
                "created_at": time.time(),
                "status": "SUBMITTED",
                "worker_heartbeat_s": config.worker_heartbeat_s,
            }
            write_json_atomic(run_dir / "run.json", run)
            if run_env:
                environment_path = run_dir / "environment.json"
                write_json_atomic(environment_path, run_env)
                environment_path.chmod(0o600)
            if not launch_worker:
                return run_id
            self._launch_worker(run_dir, run, env)
            return run_id

    def _launch_worker(
        self,
        run_dir: Path,
        run: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        worker_log = (run_dir / "worker.log").open("ab")
        worker_cmd = [
            sys.executable,
            "-m",
            "auto_research.runner_worker",
            "--run-dir",
            str(run_dir),
        ]
        process_env = os.environ.copy()
        persisted_env = read_json(run_dir / "environment.json", {}) or {}
        process_env.update(env or persisted_env or run.get("env") or {})
        # Put the interpreter environment that launched this runner first so
        # experiment Python commands resolve to the same package environment.
        interpreter_bin = str(Path(sys.executable).parent)
        current_path = process_env.get("PATH", "")
        process_env["PATH"] = interpreter_bin + (
            os.pathsep + current_path if current_path else ""
        )
        process_env["AUTO_RESEARCH_RUN_ID"] = str(run["run_id"])
        process_env["AUTO_RESEARCH_RUN_DIR"] = str(run_dir)
        process = subprocess.Popen(
            worker_cmd,
            cwd=str(Path(str(run["worktree"])).resolve()),
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
        self._update_run(
            run_dir,
            worker_pid=process.pid,
            worker_pid_start_ticks=process_start_ticks(process.pid),
            status="RUNNING",
        )

    def launch(self, run_id: str) -> dict[str, Any]:
        """Launch a durably submitted run from the host-side Supervisor."""
        run_id = _validate_run_id(run_id)
        run_dir = self.runs_dir / run_id
        if not run_dir.exists():
            raise FileNotFoundError(f"Unknown run: {run_id}")
        with _terminal_lock(self.runs_dir):
            result = self.get_result(run_id)
            if result is not None:
                return self.get_run(run_id)
            run = self.get_run(run_id)
            if run.get("status") == "RUNNING":
                return run
            if run.get("status") != "SUBMITTED":
                raise RuntimeError(
                    f"run {run_id} cannot launch from status {run.get('status')!r}"
                )
            self._launch_worker(run_dir, run)
            return self.get_run(run_id)

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
        started = read_json(run_dir / "events" / "started.json", {}) or {}
        deadline_origin = float(started.get("started_at", run.get("created_at", time.time())))
        deadline = (
            deadline_origin + int(run.get("timeout_s", 3600)) + grace_s
        )

        def terminal_or_lost() -> TerminalRunResult | None:
            result = self.get_result(run_id)
            if result:
                return result
            if time.time() >= deadline:
                current = self.get_run(run_id)
                if current.get("status") == "RUNNING":
                    result = self.reconcile_worker(run_id, now=time.time())
                    if result is not None:
                        return result
                return self._mark_lost(run_dir, current)
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

    def list_unfinished(self, *, thread_id: str) -> list[dict[str, Any]]:
        """Return durable owned runs that still require Supervisor attention."""
        unfinished: list[dict[str, Any]] = []
        for run_dir in sorted(self.runs_dir.glob("run-*")):
            run_path = run_dir / "run.json"
            if not run_path.is_file():
                raise RuntimeError(f"Missing run metadata: {run_dir.name}")
            run = read_json(run_path)
            if not isinstance(run, dict) or not run:
                raise RuntimeError(f"Invalid run metadata: {run_dir.name}")
            if run.get("codex_thread_id") != thread_id:
                continue
            if run.get("status") not in {"SUBMITTED", "RUNNING"}:
                continue
            if any((run_dir / "events" / name).exists() for name in TERMINAL_EVENT_NAMES):
                continue
            unfinished.append(run)
        return unfinished

    def reconcile_worker(
        self, run_id: str, *, now: float | None = None
    ) -> TerminalRunResult | None:
        """Reconcile exit or deadline of one Worker without guessing process state."""
        run = self.get_run(run_id)
        if run.get("status") != "RUNNING":
            return self.get_result(run_id)
        now = time.time() if now is None else now
        run_dir = self.runs_dir / run_id
        worker_state = process_identity_state(
            run.get("worker_pid"), run.get("worker_pid_start_ticks")
        )
        if worker_state == "alive":
            started = read_json(run_dir / "events" / "started.json", {}) or {}
            origin = float(started.get("started_at", run.get("created_at", now)))
            grace_s = self.config.event_grace_s if self.config else 30.0
            deadline = origin + int(run.get("timeout_s", 3600)) + grace_s
            if now < deadline:
                return None
            heartbeat = read_json(run_dir / "heartbeat.json", {}) or {}
            heartbeat_at = heartbeat.get("timestamp")
            try:
                heartbeat_age_s = max(0.0, now - float(heartbeat_at))
            except (TypeError, ValueError):
                heartbeat_age_s = None
            stale_after_s = max(
                grace_s,
                3.0 * float(run.get("worker_heartbeat_s", 5.0)),
            )
            heartbeat_state = (
                "missing"
                if heartbeat_age_s is None
                else "stale"
                if heartbeat_age_s > stale_after_s
                else "fresh"
            )
            return self._mark_timeout(
                run_dir,
                run,
                error=(
                    "Worker remained alive beyond the experiment deadline; "
                    f"heartbeat={heartbeat_state}"
                ),
            )
        if worker_state == "unverifiable":
            write_json_atomic(
                run_dir / "worker_identity.error.json",
                {
                    "run_id": run_id,
                    "error": "Worker process identity could not be verified",
                    "detected_at": time.time(),
                },
            )
            raise RuntimeError(
                f"run {run_id} Worker process identity could not be verified"
            )
        child_pid = run.get("child_pid")
        if child_pid and not terminate_process_group(
            child_pid, run.get("child_pid_start_ticks")
        ):
            write_json_atomic(
                run_dir / "lost.cleanup_error.json",
                {
                    "run_id": run_id,
                    "error": f"could not stop verified child_pid={child_pid}",
                    "detected_at": time.time(),
                },
            )
            raise RuntimeError(
                f"run {run_id} Worker exited but child process could not be stopped"
            )
        event = {
            "event": "RUN_LOST",
            "run_id": run_id,
            "idea_id": run.get("idea_id", ""),
            "status": "LOST",
            "error": "Worker exited without writing a terminal event",
            "finished_at": time.time(),
        }
        finalize_run(run_dir, "lost.json", event, run)
        return self.get_result(run_id)

    def _mark_timeout(
        self, run_dir: Path, run: dict[str, Any], *, error: str
    ) -> TerminalRunResult:
        """Commit watchdog TIMEOUT only after verified Worker/child cleanup."""
        existing = self.get_result(run["run_id"])
        if existing:
            return existing
        current = read_json(run_dir / "run.json", {}) or run
        cleanup_errors: list[str] = []
        for key in ("child_pid", "worker_pid"):
            pid = current.get(key)
            start_ticks = current.get(f"{key}_start_ticks")
            if pid and not terminate_process_group(pid, start_ticks):
                cleanup_errors.append(f"could not stop verified {key}={pid}")
        if cleanup_errors:
            write_json_atomic(
                run_dir / "timeout.cleanup_error.json",
                {
                    "run_id": run["run_id"],
                    "error": "; ".join(cleanup_errors),
                    "failed_at": time.time(),
                },
            )
            raise RuntimeError(
                f"run {run['run_id']} watchdog could not stop all processes: "
                + "; ".join(cleanup_errors)
            )
        event = {
            "event": "RUN_TIMEOUT",
            "run_id": run["run_id"],
            "idea_id": run.get("idea_id", ""),
            "status": "TIMEOUT",
            "return_code": None,
            "error": error,
            "finished_at": time.time(),
        }
        finalize_run(run_dir, "timeout.json", event, current)
        result = self.get_result(run["run_id"])
        if result is None:
            raise RuntimeError(f"Could not commit terminal state for {run['run_id']}")
        return result

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
            cleanup_errors: list[str] = []
            for key in ("child_pid", "worker_pid"):
                pid = run.get(key)
                start_ticks = run.get(f"{key}_start_ticks")
                if pid and not terminate_process_group(pid, start_ticks):
                    cleanup_errors.append(f"could not stop verified {key}={pid}")
            if cleanup_errors:
                write_json_atomic(
                    run_dir / "cancel.error.json",
                    {
                        "run_id": run_id,
                        "error": "; ".join(cleanup_errors),
                        "failed_at": time.time(),
                    },
                )
                raise RuntimeError(
                    f"run {run_id} cancellation did not stop all processes: "
                    + "; ".join(cleanup_errors)
                )
            event = {
                "event": "RUN_CANCELLED",
                "run_id": run_id,
                "idea_id": run.get("idea_id", ""),
                "status": "CANCELLED",
                "error": "cancelled by operator"
                + ("; " + "; ".join(cleanup_errors) if cleanup_errors else ""),
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
        artifact_validation = read_json(run_dir / "artifact_validation.json", {}) or {}
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
            artifact_validation=artifact_validation,
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
        current = read_json(run_dir / "run.json", {}) or run
        cleanup_errors: list[str] = []
        for key in ("child_pid", "worker_pid"):
            pid = current.get(key)
            start_ticks = current.get(f"{key}_start_ticks")
            if pid and not terminate_process_group(pid, start_ticks):
                cleanup_errors.append(f"could not stop verified {key}={pid}")
        if cleanup_errors:
            write_json_atomic(
                run_dir / "lost.cleanup_error.json",
                {
                    "run_id": run["run_id"],
                    "error": "; ".join(cleanup_errors),
                    "failed_at": time.time(),
                },
            )
            raise RuntimeError(
                f"run {run['run_id']} watchdog could not stop all processes: "
                + "; ".join(cleanup_errors)
            )
        event = {
            "event": "RUN_LOST",
            "run_id": run["run_id"],
            "idea_id": run.get("idea_id", ""),
            "status": "LOST",
            "error": "no terminal event before watchdog deadline"
            + ("; " + "; ".join(cleanup_errors) if cleanup_errors else ""),
            "finished_at": time.time(),
        }
        finalize_run(run_dir, "lost.json", event, current)
        result = self.get_result(run["run_id"])
        if result is None:
            raise RuntimeError(f"Could not commit terminal state for {run['run_id']}")
        return result

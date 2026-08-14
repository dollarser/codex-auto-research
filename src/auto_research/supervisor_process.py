"""Detached process lifecycle for the Thread-keyed Goal Supervisor."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .app_server import AppServerClient, AppServerTimeout
from .config import ResearchConfig, load_config
from .ledger import read_json, write_json_atomic
from .process_identity import (
    process_start_ticks,
    terminate_process_group,
)
from .research_session import ResearchSessionManager
from .state_paths import resolve_thread_id, thread_state_root, validate_thread_id
from .supervisor import (
    FINAL_STATES,
    STATE_SCHEMA_VERSION,
    GoalRuntimeSupervisor,
    SupervisorError,
    SupervisorOwnershipError,
    supervisor_dir,
)
from .turn_watchdog import find_turn, turn_progress_signature


@contextmanager
def _launch_lock(control: Path) -> Iterator[None]:
    path = control / "launch.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    import fcntl

    with path.open("r+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _scheduler_lock_is_held(control: Path) -> bool:
    """Detect an untracked legacy/current Supervisor before forking another."""
    path = control / "scheduler.lock"
    path.touch(exist_ok=True)
    import fcntl

    with path.open("r+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return False


def read_supervisor_process(
    project_dir: str | Path, thread_id: str
) -> dict[str, Any] | None:
    path = supervisor_dir(project_dir, thread_id) / "process.json"
    process = read_json(path, {}) or {}
    try:
        pid = int(process.get("pid"))
        recorded_start = int(process.get("pid_start_ticks"))
    except (TypeError, ValueError):
        path.unlink(missing_ok=True)
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return None
    except PermissionError:
        # A sandbox may be unable to inspect the host process. Absence of
        # permission is not evidence that the durable owner is dead.
        return None
    current_start = process_start_ticks(pid)
    if current_start is None:
        # Likewise, an inaccessible /proc must not erase the ownership record.
        return None
    if current_start == recorded_start:
        return process
    # A live PID with a different start time is an explicit reuse signal.
    path.unlink(missing_ok=True)
    return None


def spawn_supervisor(
    project_dir: str | Path,
    *,
    thread_id: str | None = None,
    retry_limited: bool = False,
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    selected_thread_id = thread_id
    if selected_thread_id is None:
        try:
            selected_thread_id = resolve_thread_id()
        except ValueError:
            selected_thread_id = None
    if selected_thread_id is None:
        raise SupervisorError(
            "Supervisor start requires --thread-id; create one first with "
            "auto-research session --create-thread"
        )
    selected_thread_id = validate_thread_id(selected_thread_id)
    root = thread_state_root(project, selected_thread_id)
    binding_path = root / "supervisor_session.json"
    if not binding_path.exists():
        raise SupervisorError(
            "Supervisor start requires a ready supervisor_session.json; "
            "run auto-research session explicitly"
        )
    control = supervisor_dir(project, selected_thread_id)
    control.mkdir(parents=True, exist_ok=True)
    with _launch_lock(control):
        existing = read_supervisor_process(project, selected_thread_id)
        if existing:
            if existing.get("status") != "OPERATIONAL":
                stopped = terminate_process_group(
                    existing.get("pid"), existing.get("pid_start_ticks")
                )
                if not stopped:
                    raise SupervisorError(
                        "live STARTING Supervisor could not be stopped; "
                        "retaining process identity"
                    )
                (control / "process.json").unlink(missing_ok=True)
                raise SupervisorError(
                    "found a live Supervisor that never became OPERATIONAL"
                )
            return {
                "status": "ALREADY_RUNNING",
                "pid": existing["pid"],
                "operational": existing.get("status") == "OPERATIONAL",
                "thread_id": selected_thread_id,
            }
        if _scheduler_lock_is_held(control):
            raise SupervisorError(
                "scheduler.lock is owned but process.json is missing or stale; "
                "refusing to start a duplicate Supervisor"
            )
        log = (control / "supervisor.log").open("ab")
        command = [
            sys.executable,
            "-m",
            "auto_research.supervisor_process",
            "--project",
            str(project),
            "--thread-id",
            selected_thread_id,
        ]
        if retry_limited:
            command.append("--retry-limited")
        process_env = os.environ.copy()
        process_env["AUTO_RESEARCH_SUPERVISOR_CHILD"] = "1"
        process_env["CODEX_THREAD_ID"] = selected_thread_id
        process = subprocess.Popen(
            command,
            cwd=project,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=process_env,
        )
        log.close()
        start_ticks = None
        for _ in range(100):
            start_ticks = process_start_ticks(process.pid)
            if start_ticks is not None or process.poll() is not None:
                break
            time.sleep(0.01)
        if start_ticks is None:
            process.terminate()
            process.wait(timeout=5)
            raise SupervisorError("could not establish Supervisor process identity")
        write_json_atomic(
            control / "process.json",
            {
                "pid": process.pid,
                "pid_start_ticks": start_ticks,
                "status": "STARTING",
                "started_at": time.time(),
                "project_root": str(project),
                "thread_id": selected_thread_id,
            },
        )
        deadline = time.monotonic() + 30.0
        operational = False
        while time.monotonic() < deadline:
            current = read_json(control / "process.json", {}) or {}
            if current.get("pid") == process.pid and current.get("status") == "OPERATIONAL":
                operational = True
                break
            if process.poll() is not None:
                break
            time.sleep(0.05)
        if not operational:
            if process.poll() is not None:
                raise SupervisorError(
                    "Supervisor exited before OPERATIONAL; inspect "
                    f"{control / 'supervisor.log'}"
                )
            raise SupervisorError("Supervisor did not become OPERATIONAL within 30 seconds")
        return {
            "status": "OPERATIONAL",
            "pid": process.pid,
            "operational": True,
            "log": str(control / "supervisor.log"),
            "thread_id": selected_thread_id,
            "state_root": str(root),
        }


def ensure_supervisor_running(
    project_dir: str | Path, *, thread_id: str
) -> dict[str, Any]:
    existing = read_supervisor_process(project_dir, thread_id)
    if existing and existing.get("status") == "OPERATIONAL":
        return {
            "status": "ALREADY_RUNNING",
            "pid": existing["pid"],
            "operational": True,
        }
    return spawn_supervisor(project_dir, thread_id=thread_id)


def restart_supervisor(
    project_dir: str | Path,
    *,
    objective: str,
    title: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    selected_thread_id = (
        validate_thread_id(thread_id)
        if thread_id is not None
        else resolve_thread_id()
    )
    control = supervisor_dir(project, selected_thread_id)
    state_path = control / "state.json"
    state = read_json(state_path, {}) or {}
    if state.get("state") not in FINAL_STATES:
        raise SupervisorError("restart requires a completed Supervisor")
    session = ResearchSessionManager(
        project, thread_id=selected_thread_id
    ).restart_goal(objective=objective, title=title)
    bound_thread_id = str(session["thread_id"])
    if state.get("thread_id") not in {None, bound_thread_id}:
        raise SupervisorError("restart session does not match the completed controller")
    write_json_atomic(
        state_path,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "project_root": str(project),
            "state": "OPEN",
            "thread_id": bound_thread_id,
            "restarted_at": time.time(),
            "previous_goal_completed_at": state.get("updated_at"),
        },
    )
    return {
        "status": "RESTARTED",
        "thread_id": bound_thread_id,
        "goal_status": session.get("goal_status"),
        "previous_goal_completed_at": state.get("updated_at"),
        "supervisor": spawn_supervisor(project, thread_id=bound_thread_id),
    }


def _report_bootstrap_failure(
    project: Path, thread_id: str, exc: BaseException
) -> dict[str, Any]:
    """Persist and report failures that occur before Supervisor construction."""
    control = supervisor_dir(project, thread_id)
    state_path = control / "state.json"
    state: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "project_root": str(project),
        "thread_id": thread_id,
        "state": "NEEDS_USER",
        "updated_at": time.time(),
        "error": f"{type(exc).__name__}: {exc}",
    }
    write_json_atomic(state_path, state)
    try:
        with AppServerClient(
            project,
            ResearchConfig(),
            client_name="auto-research-bootstrap-repair",
            client_version="0.7.0",
            managed_daemon=True,
        ) as client:
            client.initialize()
            goal = client.get_goal(thread_id)
            if isinstance(goal, dict) and goal.get("status") == "blocked":
                return state
            turn = client.start_turn(
                thread_id,
                "Auto Research Supervisor could not start. Inspect the durable "
                "control plane and repair the startup failure.\n\n"
                + "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )[-12000:],
                approval_policy="never",
            )
        state["recovery_turn_id"] = str(turn["id"])
        state["recovery_reason"] = "Supervisor bootstrap failure"
        state["updated_at"] = time.time()
        write_json_atomic(state_path, state)
    except Exception as repair_exc:  # noqa: BLE001
        state["recovery_error"] = f"{type(repair_exc).__name__}: {repair_exc}"
        state["updated_at"] = time.time()
        write_json_atomic(state_path, state)
    return state


def _wait_repair_turn(project: Path, thread_id: str, turn_id: str) -> dict[str, Any]:
    """Wait for repair progress while still reconciling durable experiments."""
    config = load_config(project)
    deadline = time.monotonic() + config.goal_turn_timeout_s
    last_progress: str | None = None
    has_progress_baseline = False
    monitor = GoalRuntimeSupervisor(project, thread_id=thread_id)
    with AppServerClient(
        project,
        config,
        client_name="auto-research-repair-monitor",
        client_version="0.7.0",
        managed_daemon=True,
    ) as client:
        client.initialize()
        while True:
            thread = client.read_thread(thread_id, include_turns=True)
            turn = find_turn(thread, turn_id)
            if turn is not None and turn.get("status") != "inProgress":
                return turn
            progress = turn_progress_signature(turn)
            if not has_progress_baseline:
                last_progress = progress
                has_progress_baseline = True
            elif progress is not None and progress != last_progress:
                last_progress = progress
                deadline = time.monotonic() + config.goal_turn_timeout_s
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    client.interrupt_turn(thread_id, turn_id)
                except Exception as exc:
                    raise TimeoutError(
                        f"repair Turn {turn_id} stalled and interrupt failed: {exc}"
                    ) from exc
                raise TimeoutError(f"repair Turn {turn_id} did not complete")
            try:
                return client.wait_turn(
                    thread_id, turn_id, timeout_s=min(5.0, remaining)
                )
            except AppServerTimeout:
                # The completion notification may have raced this connection;
                # the next authoritative thread/read closes that gap.
                monitor._launch_or_observe_experiments(client, thread_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m auto_research.supervisor_process")
    parser.add_argument("--project", default=".")
    parser.add_argument("--thread-id")
    parser.add_argument("--retry-limited", action="store_true")
    args = parser.parse_args(argv)
    project = Path(args.project).resolve()
    thread_id = (
        validate_thread_id(args.thread_id)
        if args.thread_id is not None
        else resolve_thread_id()
    )
    supervisor: GoalRuntimeSupervisor | None = None
    try:
        supervisor = GoalRuntimeSupervisor(
            project,
            thread_id=thread_id,
            allow_limited_retry=args.retry_limited,
        )
        state = supervisor.run()
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if state.get("state") == "COMPLETED" else 1
    except SupervisorOwnershipError as exc:
        # A rejected duplicate is not a failure of the current owner and must
        # never mutate its state or create a repair Turn in its Thread.
        print(json.dumps({"state": "ALREADY_OWNED", "error": str(exc)}))
        return 1
    except Exception as exc:  # noqa: BLE001 - process-level fault boundary
        state = (
            supervisor.report_fatal_error(exc)
            if supervisor is not None
            else _report_bootstrap_failure(project, thread_id, exc)
        )
        if state.get("recovery_turn_id"):
            try:
                _wait_repair_turn(
                    project, thread_id, str(state["recovery_turn_id"])
                )
                if supervisor is None:
                    supervisor = GoalRuntimeSupervisor(
                        project,
                        thread_id=thread_id,
                        allow_limited_retry=args.retry_limited,
                    )
                supervisor._write_state(
                    state="OPEN",
                    recovery_turn_id=None,
                    recovery_reason=None,
                    recovery_error=None,
                    error=None,
                )
                state = supervisor.run()
                print(json.dumps(state, ensure_ascii=False, indent=2))
                return 0 if state.get("state") == "COMPLETED" else 1
            except Exception as recovery_exc:  # noqa: BLE001
                state = supervisor._write_state(
                    state="NEEDS_USER",
                    recovery_turn_id=None,
                    recovery_error=(
                        "repair Turn could not restore Supervisor ownership: "
                        f"{type(recovery_exc).__name__}: {recovery_exc}"
                    ),
                )
                # NEEDS_USER stops Codex recovery, not experiment ownership.
                # Keep the same durable owner alive until every already-started
                # Worker reaches terminal and terminal wake handling completes.
                if supervisor._active_experiments(thread_id):
                    try:
                        state = supervisor.run()
                        print(json.dumps(state, ensure_ascii=False, indent=2))
                        return 0 if state.get("state") == "COMPLETED" else 1
                    except Exception as monitor_exc:  # noqa: BLE001
                        state = supervisor._write_state(
                            state="NEEDS_USER",
                            recovery_turn_id=None,
                            recovery_error=(
                                "experiment monitoring also failed after repair "
                                f"Turn failure: {type(monitor_exc).__name__}: "
                                f"{monitor_exc}"
                            ),
                        )
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

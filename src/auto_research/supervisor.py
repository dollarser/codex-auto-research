"""Monitor experiments while App Server's native Goal runtime owns scheduling."""

from __future__ import annotations

import os
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .app_server import AppServerClient, AppServerError, AppServerTimeout
from .config import load_config
from .ledger import read_json, write_json_atomic
from .research_session import ResearchSessionManager, read_bound_thread_id
from .run_registry import (
    add_active_run,
    list_active_runs,
    remove_active_run,
    update_active_run,
)
from .runner import ExperimentRunner
from .state_paths import resolve_thread_id, thread_state_root, validate_thread_id
from .supervisor_facts import goal_stop_state, in_progress_turn, terminal_context
from .turn_watchdog import wait_turn_while_observing_runs

STATE_SCHEMA_VERSION = 3
CONTROL_STATES = {"OPEN", "NEEDS_USER", "COMPLETED"}
OPERATOR_STATES = {"NEEDS_USER"}
FINAL_STATES = {"COMPLETED"}


class SupervisorError(RuntimeError):
    pass


class SupervisorOwnershipError(SupervisorError):
    """Raised when another live process already owns this Thread controller."""


def supervisor_dir(project_dir: str | Path, thread_id: str) -> Path:
    return thread_state_root(project_dir, thread_id) / "supervisor"


def read_supervisor_state(project_dir: str | Path, thread_id: str) -> dict[str, Any] | None:
    value = read_json(supervisor_dir(project_dir, thread_id) / "state.json", None)
    if not isinstance(value, dict):
        return None
    project = Path(project_dir).resolve()
    if value.get("thread_id") not in {None, thread_id}:
        raise SupervisorError("Supervisor state disagrees with its Thread root")
    if value.get("project_root") not in {None, str(project)}:
        raise SupervisorError("Supervisor state belongs to another project")
    return value


def is_supervisor_thread(project_dir: str | Path, thread_id: str | None) -> bool:
    if not thread_id:
        return False
    if read_bound_thread_id(project_dir, thread_id) != thread_id:
        return False
    state = read_supervisor_state(project_dir, thread_id)
    return not state or state.get("state") not in FINAL_STATES


class GoalRuntimeSupervisor:
    """Observe native Goal Turns and wake the Goal after run terminal events."""

    def __init__(
        self,
        project_dir: str | Path,
        *,
        client_factory: Callable[[], Any] | None = None,
        session_factory: Callable[[], Any] | None = None,
        runner: ExperimentRunner | None = None,
        thread_id: str | None = None,
        allow_limited_retry: bool = False,
    ):
        self.project_dir = Path(project_dir).resolve()
        self.thread_id = (
            validate_thread_id(thread_id)
            if thread_id is not None
            else resolve_thread_id()
        )
        self.state_root = thread_state_root(self.project_dir, self.thread_id)
        self.config = load_config(self.project_dir)
        self.control_dir = supervisor_dir(self.project_dir, self.thread_id)
        self.state_path = self.control_dir / "state.json"
        self.lock_path = self.control_dir / "scheduler.lock"
        self.goal_status_request_dir = self.control_dir / "goal_status_requests"
        self.goal_status_ack_dir = self.control_dir / "goal_status_acks"
        self.client_factory = client_factory or (
            lambda: AppServerClient(
                self.project_dir,
                client_name="auto-research-goal-monitor",
                client_version="0.5.0",
                managed_daemon=True,
            )
        )
        self.session_factory = session_factory or (
            lambda: ResearchSessionManager(
                self.project_dir,
                thread_id=self.thread_id,
            )
        )
        self.runner = runner or ExperimentRunner(
            self.state_root / "runs", config=self.config
        )
        self.allow_limited_retry = allow_limited_retry

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.control_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        import fcntl

        with self.lock_path.open("r+") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SupervisorOwnershipError(
                    "another Supervisor owns this Thread"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _write_state(self, **updates: Any) -> dict[str, Any]:
        previous = read_supervisor_state(self.project_dir, self.thread_id) or {}
        requested = updates.get("state")
        state_value = requested if requested in CONTROL_STATES else previous.get("state")
        if state_value not in CONTROL_STATES:
            state_value = "OPEN"
        state: dict[str, Any] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "project_root": str(self.project_dir),
            "thread_id": self.thread_id,
            "state": state_value,
            "updated_at": time.time(),
        }
        # Recovery Turn identity is control data: it prevents duplicate fallback
        # Turns after restart. Runtime phases, Goal status and run ids are derived
        # from App Server, the registry and run events and are not copied here.
        for key in ("recovery_turn_id", "recovery_reason", "recovery_error", "error"):
            value = updates.get(key, previous.get(key))
            if value is not None:
                state[key] = value
        if state_value != "NEEDS_USER" and updates.get("recovery_turn_id") is None:
            for key in ("recovery_reason", "recovery_error", "error"):
                if key in updates and updates[key] is None:
                    state.pop(key, None)
        write_json_atomic(self.state_path, state)
        return state

    def _prepare_thread(self) -> dict[str, Any]:
        session = self.session_factory().validate_existing()
        if session.get("thread_id") != self.thread_id:
            raise SupervisorError(
                "session binding disagrees with the Supervisor Thread root"
            )
        return session

    def _publish_operational(self) -> None:
        if os.environ.get("AUTO_RESEARCH_SUPERVISOR_CHILD") != "1":
            return
        process_path = self.control_dir / "process.json"
        process: dict[str, Any] = {}
        for _ in range(200):
            process = read_json(process_path, {}) or {}
            if process.get("pid") == os.getpid():
                break
            time.sleep(0.01)
        if process.get("pid") != os.getpid():
            raise SupervisorError("Supervisor process identity changed during bootstrap")
        write_json_atomic(
            process_path,
            {**process, "status": "OPERATIONAL", "operational_at": time.time()},
        )

    def _clear_active_experiment(self, run_id: str) -> None:
        remove_active_run(self.state_root, run_id)

    def _active_experiments(self, thread_id: str) -> list[dict[str, Any]]:
        return list_active_runs(self.state_root, thread_id=thread_id)

    def _reconcile_orphan_runs(self, thread_id: str) -> None:
        """Restore ownership for durable unfinished runs missing from registry."""
        registered = {
            str(item["run_id"]) for item in self._active_experiments(thread_id)
        }
        for run in self.runner.list_unfinished(thread_id=thread_id):
            run_id = str(run["run_id"])
            if run_id not in registered:
                add_active_run(self.state_root, run_id=run_id, thread_id=thread_id)

    def _apply_goal_status_request(self, client: Any, thread_id: str) -> None:
        """Bridge a self-issued Goal status request from the sandboxed shell."""
        for path in sorted(self.goal_status_request_dir.glob("*.json")):
            request = read_json(path, {}) or {}
            request_id = request.get("request_id")
            if request.get("thread_id") != thread_id or not isinstance(request_id, str):
                raise SupervisorError(f"invalid Goal status request: {path}")
            status = request.get("status")
            if status not in {"active", "paused", "blocked", "complete"}:
                raise SupervisorError(f"invalid Goal status in {path}")
            goal = client.set_goal_status(thread_id, status)
            write_json_atomic(
                self.goal_status_ack_dir / f"{request_id}.json",
                {
                    "request_id": request_id,
                    "thread_id": thread_id,
                    "requested_status": status,
                    "goal_status": goal.get("status"),
                    "requested_at": request.get("requested_at"),
                    "applied_at": time.time(),
                },
            )
            path.unlink(missing_ok=True)

    def _start_repair_turn(
        self, client: Any, thread_id: str, *, reason: str, run_id: str | None = None
    ) -> dict[str, Any] | None:
        """Ask Codex to repair a Goal-runtime failure with one ordinary Turn.

        This is deliberately a recovery path, not a second scheduler: it is used
        only after native continuation could not be made observable.  A failure
        to create even this Turn is the point at which external intervention is
        genuinely required (for example quota, authentication, or daemon loss).
        """
        details = f"reason={reason}"
        if run_id:
            details = f"run_id={run_id}\n{details}"
        prompt = (
            "Supervisor could not obtain a native Goal continuation. Inspect the "
            "current Goal, durable Supervisor state, and any injected experiment "
            "result. Repair the control-plane failure if possible, then continue "
            "the research.\n\n"
            f"{details}"
        )
        try:
            turn = client.start_turn(
                thread_id,
                prompt,
                approval_policy=self.config.codex_approval_policy,
            )
        except AppServerError as exc:
            self._write_state(
                state="NEEDS_USER",
                recovery_reason=reason,
                recovery_turn_id=None,
                recovery_error=f"{type(exc).__name__}: {exc}",
                error=(
                    "Native Goal continuation and ordinary repair Turn both failed; "
                    "check quota, authentication, and App Server availability"
                ),
            )
            return None
        self._write_state(
            state="OPEN",
            recovery_reason=reason,
            recovery_turn_id=str(turn["id"]),
            recovery_error=None,
            error=None,
        )
        return turn

    def _activate_or_start_repair(
        self, client: Any, thread_id: str, *, reason: str, run_id: str | None = None
    ) -> dict[str, Any] | None:
        """Prefer native activation; fall back to one repair Turn on rejection."""
        try:
            goal = client.get_goal(thread_id)
            if goal and goal.get("status") == "active":
                self._write_state(
                    recovery_reason=None,
                    recovery_turn_id=None,
                    recovery_error=None,
                )
                return None
            self._write_state(state="OPEN", error=None)
            activated = client.set_goal_status(thread_id, "active")
            if activated.get("status") != "active":
                raise SupervisorError(
                    "App Server did not activate Goal: "
                    f"status={activated.get('status')!r}"
                )
            self._write_state(
                recovery_reason=None,
                recovery_turn_id=None,
                recovery_error=None,
            )
            return None
        except (AppServerError, SupervisorError) as exc:
            return self._start_repair_turn(
                client,
                thread_id,
                reason=f"{reason}; activation_error={type(exc).__name__}: {exc}",
                run_id=run_id,
            )

    def _finish_experiment(
        self,
        client: Any,
        thread_id: str,
        run_id: str,
        result: dict[str, Any],
        *,
        defer_wake: bool = False,
    ) -> bool:
        """Best-effort notify one terminal result, then continue Goal handling."""
        marker = next(
            (
                item
                for item in self._active_experiments(thread_id)
                if item.get("run_id") == run_id
            ),
            {},
        )
        if not marker.get("terminal_injected_at"):
            try:
                client.inject_items(thread_id, terminal_context(result))
                update_active_run(
                    self.state_root,
                    run_id,
                    terminal_injected_at=time.time(),
                    terminal_status=result.get("status"),
                )
            except AppServerError as exc:
                # Injection is only a compact notification. The authoritative
                # terminal event and all evidence remain in the run directory,
                # which Codex inspects after wake-up. Never stop the campaign
                # merely because this optional notification was unavailable.
                write_json_atomic(
                    self.runner.runs_dir / run_id / "terminal_injection_error.json",
                    {
                        "run_id": run_id,
                        "error": f"{type(exc).__name__}: {exc}",
                        "failed_at": time.time(),
                    },
                )
        if defer_wake:
            return True
        return self._complete_terminal_delivery(client, thread_id, [run_id])

    def _complete_terminal_delivery(
        self, client: Any, thread_id: str, run_ids: list[str]
    ) -> bool:
        """Apply the native Goal rule once for a batch of injected terminals."""
        goal = client.get_goal(thread_id)
        status = goal.get("status") if isinstance(goal, dict) else None
        if status == "paused":
            fallback = self._activate_or_start_repair(
                client,
                thread_id,
                reason="experiment terminal event",
                run_id=run_ids[0] if len(run_ids) == 1 else None,
            )
            state = read_json(self.state_path, {}) or {}
            if state.get("state") == "NEEDS_USER":
                # The result is already durable in Thread history.  Retaining
                # an active-run marker solely to retry Goal activation would
                # create a second control plane for operator recovery.
                for run_id in run_ids:
                    self._clear_active_experiment(run_id)
                return False
            if fallback is not None:
                self._write_state(recovery_turn_id=str(fallback["id"]))
        elif status == "blocked":
            # A real blocked Goal is never auto-recovered, including at terminal.
            self._write_state(state="OPEN")
        elif status == "complete" or goal is None:
            self._write_state(state="COMPLETED")
        elif status == "active":
            self._write_state(state="OPEN")
        else:
            self._write_state(
                state="NEEDS_USER",
                error=f"terminal result delivered while Goal status was {status!r}",
            )
        for run_id in run_ids:
            self._clear_active_experiment(run_id)
        return True

    def _launch_or_observe_experiment(
        self,
        client: Any,
        thread_id: str,
        marker: dict[str, Any],
        *,
        defer_wake: bool = False,
    ) -> str:
        """Advance one owned run without suppressing useful continuations."""
        run_id = str(marker["run_id"])
        run = self.runner.get_run(run_id)
        if run.get("codex_thread_id") != thread_id:
            raise SupervisorError(
                f"registry run {run_id} belongs to {run.get('codex_thread_id')!r}, "
                f"not {thread_id!r}"
            )
        if run.get("status") == "SUBMITTED":
            run = self.runner.launch(run_id)
        result = self.runner.get_result(run_id)
        if result is None and run.get("status") == "RUNNING":
            result = self.runner.reconcile_worker(run_id)
        if result is not None:
            delivered = self._finish_experiment(
                client,
                thread_id,
                run_id,
                result.to_dict(),
                defer_wake=defer_wake,
            )
            if delivered and defer_wake:
                return "TERMINAL_INJECTED"
            return "TERMINAL" if delivered else "NEEDS_USER"
        return "RUNNING"

    def _launch_or_observe_experiments(
        self, client: Any, thread_id: str
    ) -> dict[str, str]:
        self._reconcile_orphan_runs(thread_id)
        dispositions: dict[str, str] = {}
        for marker in self._active_experiments(thread_id):
            run_id = str(marker["run_id"])
            dispositions[run_id] = self._launch_or_observe_experiment(
                client, thread_id, marker, defer_wake=True
            )
        injected = [
            run_id
            for run_id, disposition in dispositions.items()
            if disposition == "TERMINAL_INJECTED"
        ]
        if injected:
            delivered = self._complete_terminal_delivery(client, thread_id, injected)
            replacement = "TERMINAL" if delivered else "NEEDS_USER"
            for run_id in injected:
                dispositions[run_id] = replacement
        return dispositions

    def _observe_terminal_after_goal_stop(
        self,
        client: Any,
        thread_id: str,
        goal: dict[str, Any] | None,
        stop_status: str,
    ) -> dict[str, Any]:
        """Finish observing an already-running Worker after Goal execution stops."""
        if not self._active_experiments(thread_id):
            return self._write_state(
                state=(
                    "COMPLETED"
                    if stop_status == "complete"
                    else "NEEDS_USER"
                    if stop_status == "needs_user"
                    else "OPEN"
                )
            )
        while self._active_experiments(thread_id):
            if (read_json(self.state_path, {}) or {}).get("state") != "NEEDS_USER":
                self._apply_goal_status_request(client, thread_id)
            dispositions = self._launch_or_observe_experiments(client, thread_id)
            state = read_json(self.state_path, {}) or {}
            if state.get("state") == "NEEDS_USER" and not self._active_experiments(
                thread_id
            ):
                return state
            if "TERMINAL" in dispositions.values():
                if stop_status == "complete" and not self._active_experiments(
                    thread_id
                ):
                    return self._write_state(state="COMPLETED")
                if stop_status == "paused":
                    # The terminal path has already issued the wake. Do not decide
                    # from an immediate, potentially stale paused Goal snapshot.
                    return self._write_state(state="OPEN", error=None)
            time.sleep(self.config.event_poll_s)
        current = read_json(self.state_path, {}) or {}
        if stop_status == "needs_user" and current.get("state") != "NEEDS_USER":
            return current
        return self._write_state(
            state=(
                "COMPLETED"
                if stop_status == "complete"
                else "NEEDS_USER"
                if stop_status == "needs_user"
                else "OPEN"
            )
        )

    def _retry_limited_goal_once(
        self, client: Any, thread_id: str
    ) -> bool:
        """Perform one operator-requested retry without repair or polling loops."""
        try:
            goal = client.set_goal_status(thread_id, "active")
        except AppServerError as exc:
            self._write_state(
                state="NEEDS_USER",
                error=f"manual limited Goal retry failed: {type(exc).__name__}: {exc}",
            )
            return False
        if goal.get("status") != "active":
            self._write_state(
                state="NEEDS_USER",
                error=(
                    "manual limited Goal retry was not accepted: "
                    f"status={goal.get('status')!r}"
                ),
            )
            return False
        self._write_state(state="OPEN", error=None)
        return True

    def _after_goal_turn(
        self, client: Any, thread_id: str, completed: dict[str, Any]
    ) -> str:
        state_before = read_json(self.state_path, {}) or {}
        if state_before.get("recovery_turn_id") == completed.get("id"):
            self._write_state(
                state="OPEN",
                recovery_turn_id=None,
                recovery_reason=None,
                recovery_error=None,
                error=None,
            )
        had_runs = bool(self._active_experiments(thread_id))
        dispositions = self._launch_or_observe_experiments(client, thread_id)
        terminal_wake_requested = "TERMINAL" in dispositions.values()
        # Even while runs remain active, the just-finished Goal Turn may have
        # completed, blocked, or hit a limit. Fall through to the authoritative
        # Goal check instead of assuming another continuation will exist.
        goal = client.get_goal(thread_id)
        stop = goal_stop_state(goal)
        if stop:
            observed = self._observe_terminal_after_goal_stop(
                client, thread_id, goal, stop
            )
            if observed.get("state") == "NEEDS_USER":
                return "STOP"
            if stop == "paused" and (had_runs or terminal_wake_requested):
                # A terminal wake is confirmed by the next Goal-origin Turn,
                # not by an immediate Goal read that may still show paused.
                return "CONTINUE"
            return "STOP"
        if goal is None:
            # App Server removes a completed Goal.  Once no experiment marker
            # remains, this is a clean terminal state: a status-only
            # ``thread/goal/set`` call cannot recreate a missing Goal.
            self._write_state(
                state="COMPLETED",
                active_turn_id=None,
                recovery_reason=None,
                recovery_turn_id=None,
                error=None,
            )
            return "STOP"
        if goal.get("status") != "active":
            self._write_state(
                state="NEEDS_USER",
                error=f"unsupported Goal status {goal.get('status')!r}",
            )
            return "STOP"
        return "CONTINUE"

    def _wait_turn_while_observing_runs(
        self, client: Any, thread_id: str, turn_id: str
    ) -> dict[str, Any]:
        return wait_turn_while_observing_runs(self, client, thread_id, turn_id)

    def run(self) -> dict[str, Any]:
        """Run until the native Goal completes or reaches an operator stop."""
        with self._lock():
            # A completed controller is immutable until an explicit restart
            # operation is introduced.  In particular, do not call
            # ResearchSessionManager.prepare() first: that method is allowed to
            # recreate a missing Goal for an explicitly reused session, which
            # would otherwise leave a fresh paused Goal with no Supervisor.
            existing_state = read_json(self.state_path, {}) or {}
            if existing_state.get("state") in FINAL_STATES:
                return existing_state
            session = self._prepare_thread()
            thread_id = str(session["thread_id"])
            state = read_json(self.state_path, {}) or {}
            if state and state.get("thread_id") not in {None, thread_id}:
                raise SupervisorError("Supervisor state is bound to another thread")
            retained_state = (
                "NEEDS_USER"
                if existing_state.get("state") == "NEEDS_USER"
                and not self.allow_limited_retry
                else "OPEN"
            )
            self._write_state(thread_id=thread_id, state=retained_state)
            with self.client_factory() as client:
                client.initialize()
                thread = client.resume_thread(
                    thread_id,
                    model=self.config.codex_model,
                    approval_policy=self.config.codex_approval_policy,
                    sandbox=self.config.codex_sandbox,
                )

                self._launch_or_observe_experiments(client, thread_id)
                self._publish_operational()
                control_state = read_json(self.state_path, {}) or {}
                if control_state.get("state") == "NEEDS_USER":
                    if self._active_experiments(thread_id):
                        observed = self._observe_terminal_after_goal_stop(
                            client,
                            thread_id,
                            client.get_goal(thread_id),
                            "needs_user",
                        )
                        if observed.get("state") == "NEEDS_USER":
                            return observed
                        # Rejoin the watchdog for the activated Goal or repair Turn.
                        existing_state = observed
                    else:
                        return control_state
                # thread/resume doesn't guarantee that Turn history is included.
                # Re-read after any Goal mutation so a continuation that started
                # before this monitor subscribed isn't mistaken for a wake failure.
                thread = client.read_thread(thread_id, include_turns=True)

                in_progress = in_progress_turn(thread)
                if in_progress is None:
                    recovery_turn_id = (read_json(self.state_path, {}) or {}).get(
                        "recovery_turn_id"
                    )
                    if isinstance(recovery_turn_id, str) and recovery_turn_id:
                        in_progress = {"id": recovery_turn_id, "status": "inProgress"}
                limited_retry = False
                if in_progress is None:
                    goal = client.get_goal(thread_id)
                    stop = goal_stop_state(goal)
                    retry_paused_after_needs_user = (
                        stop == "paused"
                        and self.allow_limited_retry
                        and existing_state.get("state") == "NEEDS_USER"
                    )
                    if self.allow_limited_retry and (
                        stop == "needs_user" or retry_paused_after_needs_user
                    ):
                        if not self._retry_limited_goal_once(client, thread_id):
                            return read_json(self.state_path, {}) or {}
                        goal = {"status": "active"}
                        stop = None
                        limited_retry = True
                    activate_fresh_goal = stop == "paused" and not existing_state
                    if activate_fresh_goal:
                        # A newly created research Goal starts paused so the
                        # monitor can subscribe before its first activation.
                        stop = None
                    if stop:
                        had_runs = bool(self._active_experiments(thread_id))
                        observed = self._observe_terminal_after_goal_stop(
                            client, thread_id, goal, stop
                        )
                        if observed.get("state") == "NEEDS_USER":
                            return observed
                        if stop not in {"blocked", "paused"}:
                            return observed
                        if not had_runs or stop == "blocked":
                            return observed
                        # Terminal handling already requested activation. Wait
                        # for its Goal Turn even if persisted status is briefly
                        # still paused.
                        goal = {"status": "active"}
                    if not goal or goal.get("status") != "active":
                        if activate_fresh_goal:
                            in_progress = self._activate_or_start_repair(
                                client,
                                thread_id,
                                reason="activate newly created paused Goal",
                            )
                            state = read_json(self.state_path, {}) or {}
                            if state.get("state") == "NEEDS_USER":
                                return state
                        else:
                            self._write_state(
                                state="NEEDS_USER",
                                error=(
                                    "Supervisor bootstrap found unsupported Goal status "
                                    f"{goal.get('status') if isinstance(goal, dict) else None!r}"
                                ),
                            )
                            return read_json(self.state_path, {}) or {}

                while True:
                    self._apply_goal_status_request(client, thread_id)
                    if in_progress is None:
                        try:
                            in_progress = client.wait_turn_started(
                                thread_id, timeout_s=120
                            )
                        except AppServerTimeout as exc:
                            if limited_retry:
                                self._write_state(
                                    state="NEEDS_USER",
                                    error=(
                                        "manual limited Goal retry became active but "
                                        f"started no continuation: {exc}"
                                    ),
                                )
                                return read_json(self.state_path, {}) or {}
                            in_progress = self._start_repair_turn(
                                client,
                                thread_id,
                                reason=(
                                    "Goal became active but App Server emitted no "
                                    f"automatic turn/started: {exc}"
                                ),
                            )
                            if in_progress is None:
                                return read_json(self.state_path, {}) or {}
                        limited_retry = False
                    turn_id = str(in_progress["id"])
                    completed = self._wait_turn_while_observing_runs(
                        client, thread_id, turn_id
                    )
                    in_progress = None
                    repair_turn_id = completed.get("supervisor_repair_turn_id")
                    if isinstance(repair_turn_id, str) and repair_turn_id:
                        in_progress = {
                            "id": repair_turn_id,
                            "status": "inProgress",
                        }
                        continue
                    state = read_json(self.state_path, {}) or {}
                    if state.get("state") == "NEEDS_USER":
                        if self._active_experiments(thread_id):
                            return self._observe_terminal_after_goal_stop(
                                client,
                                thread_id,
                                client.get_goal(thread_id),
                                "needs_user",
                            )
                        return state
                    if self._after_goal_turn(client, thread_id, completed) == "STOP":
                        return read_json(self.state_path, {}) or {}
                    state = read_json(self.state_path, {}) or {}
                    if state.get("state") == "NEEDS_USER":
                        return state
                    recovery_turn_id = state.get("recovery_turn_id")
                    if isinstance(recovery_turn_id, str) and recovery_turn_id:
                        in_progress = {
                            "id": recovery_turn_id,
                            "status": "inProgress",
                        }

    def resume(self) -> dict[str, Any]:
        state = read_json(self.state_path, {}) or {}
        if state.get("state") not in OPERATOR_STATES:
            raise SupervisorError("Supervisor is not in an operator-paused state")
        return self._write_state(
            state="OPEN",
            recovery_turn_id=None,
            recovery_reason=None,
            recovery_error=None,
            error=None,
        )

    def report_fatal_error(self, exc: BaseException) -> dict[str, Any]:
        """Persist an unexpected failure and make one best-effort repair Turn."""
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        state = self._write_state(
            state="NEEDS_USER",
            error=f"{type(exc).__name__}: {exc}",
        )
        try:
            with self.client_factory() as client:
                client.initialize()
                goal = client.get_goal(self.thread_id)
                if isinstance(goal, dict) and goal.get("status") == "blocked":
                    return state
                turn = client.start_turn(
                    self.thread_id,
                    "Auto Research Supervisor stopped unexpectedly. Inspect and repair "
                    "the durable control plane, then restart the Supervisor without "
                    "resubmitting an already durable run.\n\n"
                    f"{detail[-12000:]}",
                    approval_policy=self.config.codex_approval_policy,
                )
            state = self._write_state(
                state="NEEDS_USER",
                recovery_turn_id=str(turn["id"]),
                recovery_reason="unexpected Supervisor failure",
            )
        except Exception as repair_exc:  # noqa: BLE001 - durable error remains authoritative
            state = self._write_state(
                state="NEEDS_USER",
                recovery_error=f"{type(repair_exc).__name__}: {repair_exc}",
            )
        return state


# Public orchestration name.
AppServerSupervisor = GoalRuntimeSupervisor

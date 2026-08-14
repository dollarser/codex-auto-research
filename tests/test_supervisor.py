from __future__ import annotations

import io
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from collections import deque
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from auto_research.app_server import AppServerClient, AppServerError, AppServerTimeout
from auto_research.ledger import write_json_atomic
from auto_research.mcp_server import ExperimentService
from auto_research.process_identity import process_start_ticks
from auto_research.run_registry import (
    RegistryCorruptionError,
    add_active_run,
    list_active_runs,
    remove_active_run,
)
from auto_research.runner import ExperimentRunner, finalize_run
from auto_research.state_paths import thread_state_root
from auto_research.supervisor import (
    GoalRuntimeSupervisor as _GoalRuntimeSupervisor,
)
from auto_research.supervisor import (
    SupervisorError,
)
from auto_research.supervisor import (
    supervisor_dir as _supervisor_dir,
)


def GoalRuntimeSupervisor(project: Path, *args, **kwargs):
    kwargs.setdefault("thread_id", "thread-goal")
    return _GoalRuntimeSupervisor(project, *args, **kwargs)


def supervisor_dir(project: Path, thread_id: str = "thread-goal") -> Path:
    return _supervisor_dir(project, thread_id)


def write_goal(project: Path) -> None:
    project.joinpath("GOAL.md").write_text(
        "# Goal\n\nImprove score.\n",
        encoding="utf-8",
    )
    write_json_atomic(
        thread_state_root(project, "thread-goal") / "supervisor_session.json",
        {
            "schema_version": 2,
            "project_root": str(project.resolve()),
            "thread_id": "thread-goal",
            "title": "Test research",
            "objective": "Improve score.",
            "ownership": "adopted",
            "setup_state": "ready",
            "current_cycle_id": "cycle-test",
        },
    )


def write_terminal_run(
    project: Path,
    *,
    thread_id: str = "thread-goal",
    run_id: str = "run-native-goal-test",
) -> tuple[ExperimentRunner, str]:
    root = thread_state_root(project, thread_id)
    runner = ExperimentRunner(root / "runs")
    run_dir = runner.runs_dir / run_id
    (run_dir / "events").mkdir(parents=True)
    run = {
        "run_id": run_id,
        "idea_id": "native-goal",
        "worktree": str(project),
        "command": "python train.py",
        "argv": ["python", "train.py"],
        "timeout_s": 60,
        "created_at": time.time(),
        "status": "RUNNING",
        "codex_thread_id": thread_id,
    }
    write_json_atomic(run_dir / "run.json", run)
    finalize_run(
        run_dir,
        "completed.json",
        {
            "event": "RUN_COMPLETED",
            "run_id": run_id,
            "idea_id": "native-goal",
            "status": "COMPLETED",
            "return_code": 0,
            "finished_at": time.time(),
        },
        run,
    )
    add_active_run(root, run_id=run_id, thread_id=thread_id)
    return runner, run_id


def register_run(
    project: Path,
    run_id: str,
    *,
    thread_id: str = "thread-goal",
) -> None:
    root = thread_state_root(project, thread_id)
    add_active_run(root, run_id=run_id, thread_id=thread_id)


class FakeSession:
    def __init__(self, thread_id: str):
        self.thread_id = thread_id

    def validate_existing(self):
        return {"thread_id": self.thread_id}


class FakeGoalClient:
    def __init__(self, *, on_turn_completed=None):
        self.goal = {"threadId": "thread-goal", "status": "paused"}
        self.on_turn_completed = on_turn_completed
        self.turn_count = 0
        self.started: deque[dict] = deque()
        self.calls: list[tuple] = []
        self.injected: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def initialize(self):
        self.calls.append(("initialize",))

    def require_model(self, model):
        self.calls.append(("require_model", model))
        return {"id": model, "model": model, "isDefault": True}

    def resume_thread(
        self, thread_id, *, model=None, approval_policy=None, sandbox=None
    ):
        self.calls.append(("resume", thread_id, model, approval_policy, sandbox))
        return {"id": thread_id, "turns": []}

    def read_thread(self, thread_id, *, include_turns=False):
        return {"id": thread_id, "turns": []}

    def get_goal(self, thread_id):
        return dict(self.goal)

    def set_goal_status(self, thread_id, status):
        self.goal["status"] = status
        self.calls.append(("goal", thread_id, status))
        if status == "active":
            self.turn_count += 1
            self.started.append(
                {"id": f"goal-turn-{self.turn_count}", "status": "inProgress"}
            )
        return dict(self.goal)

    def wait_turn_started(self, thread_id, *, timeout_s=None):
        self.calls.append(("wait-started", thread_id))
        return self.started.popleft()

    def wait_turn(self, thread_id, turn_id, *, timeout_s=None):
        self.calls.append(("wait-completed", thread_id, turn_id))
        if self.on_turn_completed:
            self.on_turn_completed(self, self.turn_count)
        if self.goal.get("status") == "active":
            self.turn_count += 1
            self.started.append(
                {"id": f"goal-turn-{self.turn_count}", "status": "inProgress"}
            )
        return {"id": turn_id, "status": "completed"}

    def interrupt_turn(self, thread_id, turn_id):
        self.calls.append(("turn/interrupt", thread_id, turn_id))

    def inject_items(self, thread_id, items):
        self.injected.extend(items)
        self.calls.append(("inject", thread_id))

    def start_turn(self, *args, **kwargs):
        raise AssertionError("Supervisor must not call turn/start")


class RefusingGoalActivationClient(FakeGoalClient):
    """App Server client that leaves a blocked Goal blocked."""

    def set_goal_status(self, thread_id, status):
        self.calls.append(("goal", thread_id, status))
        return dict(self.goal)

    def start_turn(self, thread_id, text, **kwargs):
        self.calls.append(("turn/start", thread_id, text, kwargs))
        return {"id": "fallback-repair-turn", "status": "inProgress"}


class NoNativeContinuationClient(FakeGoalClient):
    """Goal activation succeeds but the daemon never emits turn/started."""

    def set_goal_status(self, thread_id, status):
        self.goal["status"] = status
        self.calls.append(("goal", thread_id, status))
        return dict(self.goal)

    def wait_turn_started(self, thread_id, *, timeout_s=None):
        self.calls.append(("wait-started", thread_id))
        raise AppServerTimeout("no native continuation")

    def start_turn(self, thread_id, text, **kwargs):
        self.calls.append(("turn/start", thread_id, text, kwargs))
        return {"id": "repair-after-native-timeout", "status": "inProgress"}

    def wait_turn(self, thread_id, turn_id, *, timeout_s=None):
        self.calls.append(("wait-completed", thread_id, turn_id))
        self.goal["status"] = "complete"
        return {"id": turn_id, "status": "completed"}


class TurnPollingClient(FakeGoalClient):
    """Makes one bounded wait expire before returning a completed Turn."""

    def __init__(self):
        super().__init__()
        self.wait_attempts = 0

    def wait_turn(self, thread_id, turn_id, *, timeout_s=None):
        self.wait_attempts += 1
        self.calls.append(("wait-completed", thread_id, turn_id, timeout_s))
        if self.wait_attempts == 1:
            raise AppServerTimeout("bounded wait expired")
        return {"id": turn_id, "status": "completed"}


class UnrecoverableGoalClient(RefusingGoalActivationClient):
    """Neither native activation nor an ordinary repair Turn is available."""

    def start_turn(self, *args, **kwargs):
        raise AppServerError("usage limit prevents repair Turn")


class MissingGoalClient(FakeGoalClient):
    """Models an App Server that removes a Goal after completion."""

    def get_goal(self, thread_id):
        return None

    def set_goal_status(self, *args, **kwargs):
        raise AssertionError("a missing Goal must not be reactivated")


class InjectionFailureClient(FakeGoalClient):
    def inject_items(self, thread_id, items):
        raise AppServerError("injection unavailable")


class PersistedPausedActivationClient(FakeGoalClient):
    """Queues native continuations while Goal reads briefly remain paused."""

    def set_goal_status(self, thread_id, status):
        self.calls.append(("goal", thread_id, status))
        if status == "active":
            self.turn_count += 1
            self.started.append(
                {"id": f"goal-turn-{self.turn_count}", "status": "inProgress"}
            )
            return {**self.goal, "status": "active"}
        self.goal["status"] = status
        return dict(self.goal)


class SupervisorTests(unittest.TestCase):
    def test_registry_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = thread_state_root(Path(tmp), "thread-goal")
            path = root / "supervisor" / "active_experiments.json"
            path.parent.mkdir(parents=True)
            path.write_text("{not-json", encoding="utf-8")

            with self.assertRaises(RegistryCorruptionError):
                list_active_runs(root)

    def test_goal_status_bridge_processes_each_request_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = FakeGoalClient()
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            )
            write_json_atomic(
                supervisor.goal_status_request_dir / "1-first.json",
                {
                    "request_id": "first",
                    "thread_id": "thread-goal",
                    "status": "paused",
                    "requested_at": 1,
                },
            )
            write_json_atomic(
                supervisor.goal_status_request_dir / "2-second.json",
                {
                    "request_id": "second",
                    "thread_id": "thread-goal",
                    "status": "active",
                    "requested_at": 2,
                },
            )

            supervisor._apply_goal_status_request(client, "thread-goal")

            self.assertEqual(client.goal["status"], "active")
            self.assertTrue((supervisor.goal_status_ack_dir / "first.json").is_file())
            self.assertTrue((supervisor.goal_status_ack_dir / "second.json").is_file())
            self.assertFalse(list(supervisor.goal_status_request_dir.glob("*.json")))

    def test_terminal_injection_failure_is_diagnostic_and_still_wakes(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner, run_id = write_terminal_run(project)
            client = InjectionFailureClient()
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            )

            delivered = supervisor._finish_experiment(
                client, "thread-goal", run_id, runner.get_result(run_id).to_dict()
            )

            self.assertTrue(delivered)
            self.assertIn(("goal", "thread-goal", "active"), client.calls)
            self.assertFalse(
                list_active_runs(thread_state_root(project, "thread-goal"))
            )
            self.assertEqual(
                json.loads((supervisor.state_path).read_text())["state"],
                "OPEN",
            )
            self.assertTrue(
                (
                    runner.runs_dir
                    / run_id
                    / "terminal_injection_error.json"
                ).is_file()
            )

    def test_manual_limited_retry_activates_once_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)

            def complete_goal(client, _turn_count):
                client.goal["status"] = "complete"

            client = FakeGoalClient(on_turn_completed=complete_goal)
            client.goal["status"] = "usageLimited"
            result = GoalRuntimeSupervisor(
                project,
                thread_id="thread-goal",
                allow_limited_retry=True,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            ).run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(
                client.calls.count(("goal", "thread-goal", "active")), 1
            )

    def test_rejected_manual_limited_retry_stops_without_repair_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = RefusingGoalActivationClient()
            client.goal["status"] = "budgetLimited"
            result = GoalRuntimeSupervisor(
                project,
                thread_id="thread-goal",
                allow_limited_retry=True,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            ).run()

            self.assertEqual(result["state"], "NEEDS_USER")
            self.assertEqual(
                client.calls.count(("goal", "thread-goal", "active")), 1
            )
            self.assertFalse(any(call[0] == "turn/start" for call in client.calls))

    def test_manual_retry_recovers_paused_goal_after_needs_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)

            def complete_goal(client, _turn_count):
                client.goal["status"] = "complete"

            client = FakeGoalClient(on_turn_completed=complete_goal)
            client.goal["status"] = "paused"
            supervisor = GoalRuntimeSupervisor(
                project,
                thread_id="thread-goal",
                allow_limited_retry=True,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            )
            supervisor._write_state(state="NEEDS_USER", error="quota exhausted")

            result = supervisor.run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(
                client.calls.count(("goal", "thread-goal", "active")), 1
            )

    def test_injected_terminal_does_not_retain_marker_for_failed_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner, run_id = write_terminal_run(project)
            client = UnrecoverableGoalClient()
            client.goal["status"] = "paused"
            supervisor = GoalRuntimeSupervisor(
                project,
                thread_id="thread-goal",
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            )

            dispositions = supervisor._launch_or_observe_experiments(
                client, "thread-goal"
            )

            self.assertEqual(dispositions[run_id], "NEEDS_USER")
            self.assertEqual(
                json.loads(supervisor.state_path.read_text())["state"],
                "NEEDS_USER",
            )
            self.assertFalse(
                list_active_runs(thread_state_root(project, "thread-goal"))
            )

    def test_orphan_submitted_run_is_reconciled_and_launched(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner = ExperimentRunner(thread_state_root(project, "thread-goal") / "runs")
            run_id = runner.submit(
                "orphan",
                project,
                f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'",
                60,
                codex_thread_id="thread-goal",
                launch_worker=False,
            )
            supervisor = GoalRuntimeSupervisor(
                project,
                thread_id="thread-goal",
                client_factory=FakeGoalClient,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            )

            dispositions = supervisor._launch_or_observe_experiments(
                FakeGoalClient(), "thread-goal"
            )

            self.assertEqual(dispositions[run_id], "RUNNING")
            self.assertEqual(runner.get_run(run_id)["status"], "RUNNING")
            runner.cancel(run_id)

    def test_needs_user_still_launches_monitors_and_delivers_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner = ExperimentRunner(thread_state_root(project, "thread-goal") / "runs")
            run_id = runner.submit(
                "needs-user-run",
                project,
                "true",
                30,
                codex_thread_id="thread-goal",
                launch_worker=False,
            )
            register_run(project, run_id)
            client = FakeGoalClient()
            client.goal["status"] = "budgetLimited"
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            )
            supervisor._write_state(state="NEEDS_USER", error="Goal repair required")

            first = supervisor._launch_or_observe_experiments(client, "thread-goal")
            terminal = runner.wait(run_id, poll_s=0.02)
            second = supervisor._launch_or_observe_experiments(client, "thread-goal")

            self.assertIn(first[run_id], {"RUNNING", "TERMINAL"})
            self.assertEqual(terminal.status, "COMPLETED")
            self.assertEqual(second.get(run_id, "TERMINAL"), "TERMINAL")
            self.assertEqual(
                json.loads(supervisor.state_path.read_text())["state"], "NEEDS_USER"
            )
            self.assertFalse(list_active_runs(thread_state_root(project, "thread-goal")))
            self.assertTrue(client.injected)
            self.assertNotIn(("goal", "thread-goal", "active"), client.calls)

    def test_terminal_paused_goal_wakes_even_when_local_state_needs_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner, run_id = write_terminal_run(project)
            client = FakeGoalClient()
            client.goal["status"] = "paused"
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            )
            supervisor._write_state(state="NEEDS_USER", error="earlier failure")

            dispositions = supervisor._launch_or_observe_experiments(
                client, "thread-goal"
            )

            self.assertEqual(dispositions[run_id], "TERMINAL")
            self.assertIn(("goal", "thread-goal", "active"), client.calls)
            self.assertEqual(
                json.loads(supervisor.state_path.read_text())["state"], "OPEN"
            )

    def test_multiple_terminal_runs_inject_all_and_activate_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner, _ = write_terminal_run(project, run_id="run-terminal-one")
            write_terminal_run(project, run_id="run-terminal-two")
            client = FakeGoalClient()
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            )

            dispositions = supervisor._launch_or_observe_experiments(
                client, "thread-goal"
            )

            self.assertEqual(set(dispositions.values()), {"TERMINAL"})
            self.assertEqual(client.calls.count(("goal", "thread-goal", "active")), 1)
            self.assertEqual(len(client.injected), 2)
            text = client.injected[0]["content"][0]["text"]
            payload = json.loads(text.split("\n\n", 1)[1])
            self.assertEqual(
                set(payload), {"run_id", "status", "result_dir"}
            )
            self.assertFalse(list_active_runs(thread_state_root(project, "thread-goal")))

    def test_batch_injection_failure_still_wakes_and_clears_markers(self):
        class FailSecondInjectionClient(FakeGoalClient):
            def inject_items(self, thread_id, items):
                if self.injected:
                    raise AppServerError("second injection failed")
                super().inject_items(thread_id, items)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner, _ = write_terminal_run(project, run_id="run-terminal-one")
            write_terminal_run(project, run_id="run-terminal-two")
            client = FailSecondInjectionClient()
            supervisor = GoalRuntimeSupervisor(
                project,
                thread_id="thread-goal",
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            )

            dispositions = supervisor._launch_or_observe_experiments(
                client, "thread-goal"
            )

            self.assertEqual(set(dispositions.values()), {"TERMINAL"})
            self.assertEqual(
                client.calls.count(("goal", "thread-goal", "active")), 1
            )
            self.assertEqual(
                json.loads(supervisor.state_path.read_text())["state"],
                "OPEN",
            )
            self.assertFalse(
                list_active_runs(thread_state_root(project, "thread-goal"))
            )
            self.assertTrue(
                (
                    runner.runs_dir
                    / "run-terminal-two"
                    / "terminal_injection_error.json"
                ).is_file()
            )

    def test_unexpected_supervisor_failure_creates_repair_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = RefusingGoalActivationClient()
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            )

            state = supervisor.report_fatal_error(RuntimeError("registry unavailable"))

            self.assertEqual(state["state"], "NEEDS_USER")
            self.assertEqual(state["recovery_turn_id"], "fallback-repair-turn")
            self.assertTrue(any(call[0] == "turn/start" for call in client.calls))

    def test_completed_repair_turn_clears_recovery_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = FakeGoalClient()
            client.goal["status"] = "blocked"
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            )
            supervisor._write_state(
                state="OPEN",
                recovery_turn_id="repair-turn",
                recovery_reason="test",
            )

            disposition = supervisor._after_goal_turn(
                client,
                "thread-goal",
                {"id": "repair-turn", "status": "completed"},
            )

            state = json.loads(supervisor.state_path.read_text())
            self.assertEqual(disposition, "STOP")
            self.assertNotIn("recovery_turn_id", state)

    def test_unexpected_failure_does_not_override_blocked_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = RefusingGoalActivationClient()
            client.goal["status"] = "blocked"
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            )

            state = supervisor.report_fatal_error(RuntimeError("registry unavailable"))

            self.assertEqual(state["state"], "NEEDS_USER")
            self.assertFalse(any(call[0] == "turn/start" for call in client.calls))

    def test_state_write_persists_only_control_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_json_atomic(
                supervisor_dir(project) / "state.json",
                {
                    "schema_version": 3,
                    "thread_id": "thread-goal",
                    "state": "OPEN",
                    "run_id": "run-previous",
                    "active_turn_id": "turn-previous",
                    "foreign_thread_id": "thread-foreign",
                },
            )
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: FakeGoalClient(),
                session_factory=lambda: FakeSession("thread-goal"),
            )

            state = supervisor._write_state(state="OPEN", run_id="run-current")

            self.assertEqual(state["state"], "OPEN")
            self.assertNotIn("run_id", state)
            self.assertNotIn("active_turn_id", state)
            self.assertNotIn("foreign_thread_id", state)
            persisted = json.loads(
                (supervisor_dir(project) / "state.json").read_text()
            )
            self.assertEqual(persisted["schema_version"], 3)

            state = supervisor._write_state(state="OPEN")
            self.assertEqual(state["state"], "OPEN")

    def test_completed_controller_does_not_implicitly_reprepare_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_json_atomic(
                supervisor_dir(project, "thread-completed") / "state.json",
                {
                    "schema_version": 3,
                    "thread_id": "thread-completed",
                    "state": "COMPLETED",
                },
            )

            def unexpected_session_prepare():
                raise AssertionError("completed controller must not prepare a session")

            result = GoalRuntimeSupervisor(
                project,
                thread_id="thread-completed",
                session_factory=unexpected_session_prepare,
            ).run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(result["thread_id"], "thread-completed")

    def test_namespaced_bootstrap_ignores_default_active_marker(self):
        """A fresh controller must never adopt a run from the default namespace."""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            add_active_run(
                project / "research",
                run_id="run-from-default-controller",
                thread_id="other",
            )
            client = FakeGoalClient()
            client.goal["status"] = "complete"
            result = GoalRuntimeSupervisor(
                project,
                thread_id="thread-fresh",
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-fresh"),
            ).run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(result["thread_id"], "thread-fresh")

    def test_missing_goal_after_completed_turn_is_clean_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = MissingGoalClient()
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            )

            disposition = supervisor._after_goal_turn(
                client, "thread-goal", {"id": "turn-complete", "status": "completed"}
            )

            state = json.loads((supervisor_dir(project) / "state.json").read_text())
            self.assertEqual(disposition, "STOP")
            self.assertEqual(state["state"], "COMPLETED")
            self.assertNotIn("recovery_turn_id", state)
            self.assertFalse(any(call[0] == "goal" for call in client.calls))

    def test_submitted_run_launches_while_goal_turn_is_still_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner = ExperimentRunner(thread_state_root(project, "thread-goal") / "runs")
            run_id = runner.submit(
                "during-turn",
                project,
                "true",
                30,
                codex_thread_id="thread-goal",
                launch_worker=False,
            )
            write_json_atomic(
                supervisor_dir(project) / "state.json",
                {
                    "schema_version": 3,
                    "thread_id": "thread-goal",
                    "state": "OPEN",
                },
            )
            register_run(project, run_id)
            client = TurnPollingClient()
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            )

            completed = supervisor._wait_turn_while_observing_runs(
                client, "thread-goal", "goal-turn"
            )

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(client.wait_attempts, 2)
            self.assertNotEqual(runner.get_run(run_id)["status"], "SUBMITTED")
            self.assertIsNotNone(runner.get_run(run_id).get("worker_pid"))
            self.assertIn(
                runner.wait(run_id, poll_s=0.02).status, {"COMPLETED", "FAILED"}
            )

    def test_stalled_goal_turn_is_interrupted_and_gets_one_repair_turn(self):
        class StallingClient(FakeGoalClient):
            def __init__(self):
                super().__init__()
                self.goal["status"] = "active"

            def wait_turn(self, thread_id, turn_id, *, timeout_s=None):
                raise AppServerTimeout("still running")

            def read_thread(self, thread_id, *, include_turns=False):
                return {
                    "id": thread_id,
                    "turns": [{"id": "stalled-turn", "status": "inProgress"}],
                }

            def start_turn(self, thread_id, text, **kwargs):
                self.calls.append(("turn/start", thread_id))
                return {"id": "repair-turn", "status": "inProgress"}

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = StallingClient()
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            )
            supervisor.config = replace(supervisor.config, goal_turn_timeout_s=0.01)

            result = supervisor._wait_turn_while_observing_runs(
                client, "thread-goal", "stalled-turn"
            )

            self.assertEqual(result["supervisor_repair_turn_id"], "repair-turn")
            self.assertIn(("turn/interrupt", "thread-goal", "stalled-turn"), client.calls)
            self.assertEqual(client.calls.count(("turn/start", "thread-goal")), 1)

    def test_goal_turn_progress_refreshes_watchdog_deadline(self):
        class ProgressingClient(FakeGoalClient):
            def __init__(self):
                super().__init__()
                self.goal["status"] = "active"
                self.wait_attempts = 0
                self.read_attempts = 0

            def wait_turn(self, thread_id, turn_id, *, timeout_s=None):
                self.wait_attempts += 1
                if self.wait_attempts < 3:
                    time.sleep(0.02)
                    raise AppServerTimeout("poll")
                return {"id": turn_id, "status": "completed"}

            def read_thread(self, thread_id, *, include_turns=False):
                self.read_attempts += 1
                return {
                    "id": thread_id,
                    "turns": [
                        {
                            "id": "progressing-turn",
                            "status": "inProgress",
                            "items": list(range(self.read_attempts)),
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = ProgressingClient()
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            )
            supervisor.config = replace(supervisor.config, goal_turn_timeout_s=0.03)

            result = supervisor._wait_turn_while_observing_runs(
                client, "thread-goal", "progressing-turn"
            )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(client.wait_attempts, 3)
            self.assertFalse(any(call[0] == "turn/interrupt" for call in client.calls))

    def test_stalled_repair_turn_stops_without_recursive_repair(self):
        class StallingRepairClient(FakeGoalClient):
            def wait_turn(self, thread_id, turn_id, *, timeout_s=None):
                raise AppServerTimeout("still running")

            def read_thread(self, thread_id, *, include_turns=False):
                return {
                    "id": thread_id,
                    "turns": [{"id": "repair-turn", "status": "inProgress"}],
                }

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = StallingRepairClient()
            client.goal["status"] = "paused"
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            )
            supervisor.config = replace(supervisor.config, goal_turn_timeout_s=0.01)
            supervisor._write_state(state="OPEN", recovery_turn_id="repair-turn")

            supervisor._wait_turn_while_observing_runs(
                client, "thread-goal", "repair-turn"
            )

            state = json.loads(supervisor.state_path.read_text())
            self.assertEqual(state["state"], "NEEDS_USER")
            self.assertFalse(any(call[0] == "turn/start" for call in client.calls))

    def test_terminal_repair_turn_rejoins_watchdog_after_needs_user(self):
        class TerminalRepairClient(FakeGoalClient):
            def read_thread(self, thread_id, *, include_turns=False):
                return {
                    "id": thread_id,
                    "turns": [
                        {"id": "terminal-repair", "status": "inProgress"}
                    ],
                }

            def wait_turn(self, thread_id, turn_id, *, timeout_s=None):
                self.calls.append(("wait-completed", thread_id, turn_id))
                self.goal["status"] = "complete"
                return {"id": turn_id, "status": "completed"}

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            register_run(project, "run-finishing")
            client = TerminalRepairClient()
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            )
            supervisor._write_state(state="NEEDS_USER")
            launch_calls = 0

            def launch_or_observe(_client, _thread_id):
                nonlocal launch_calls
                launch_calls += 1
                if launch_calls == 1:
                    return {}
                if launch_calls == 2:
                    supervisor._write_state(
                        state="OPEN", recovery_turn_id="terminal-repair"
                    )
                    supervisor._clear_active_experiment("run-finishing")
                    return {"run-finishing": "TERMINAL"}
                return {}

            with patch.object(
                supervisor,
                "_launch_or_observe_experiments",
                side_effect=launch_or_observe,
            ):
                result = supervisor.run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertIn(
                ("wait-completed", "thread-goal", "terminal-repair"), client.calls
            )

    def test_blocked_goal_without_experiment_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = FakeGoalClient()
            client.goal["status"] = "blocked"
            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            ).run()

            self.assertEqual(result["state"], "OPEN")
            self.assertNotIn(("goal", "thread-goal", "active"), client.calls)
            self.assertEqual(client.turn_count, 0)

    def test_restart_does_not_convert_blocked_goal_to_paused(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner, _ = write_terminal_run(project)
            client = FakeGoalClient()
            client.goal["status"] = "blocked"

            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            ).run()

            self.assertEqual(result["state"], "OPEN")
            self.assertNotIn(("goal", "thread-goal", "paused"), client.calls)
            self.assertNotIn(("goal", "thread-goal", "active"), client.calls)
            self.assertTrue(client.injected)

    def test_goal_turn_that_blocks_without_experiment_is_not_reactivated(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)

            def block_goal(client, _turn_count):
                client.goal["status"] = "blocked"

            client = FakeGoalClient(on_turn_completed=block_goal)
            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            ).run()

            self.assertEqual(result["state"], "OPEN")
            self.assertEqual(client.turn_count, 1)
            self.assertEqual(
                client.calls.count(("goal", "thread-goal", "active")), 1
            )

    def test_goal_turn_that_pauses_without_experiment_is_not_reactivated(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)

            def pause_goal(client, _turn_count):
                client.goal["status"] = "paused"

            client = FakeGoalClient(on_turn_completed=pause_goal)
            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            ).run()

            self.assertEqual(result["state"], "OPEN")
            self.assertEqual(client.turn_count, 1)
            self.assertEqual(
                client.calls.count(("goal", "thread-goal", "active")), 1
            )

    def test_existing_paused_goal_is_not_reactivated_on_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_json_atomic(
                supervisor_dir(project) / "state.json",
                {
                    "schema_version": 3,
                    "thread_id": "thread-goal",
                    "state": "OPEN",
                },
            )
            client = FakeGoalClient()
            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            ).run()

            self.assertEqual(result["state"], "OPEN")
            self.assertNotIn(("goal", "thread-goal", "active"), client.calls)
            self.assertEqual(client.turn_count, 0)

    def test_native_continuation_timeout_uses_repair_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = NoNativeContinuationClient()
            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            ).run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertTrue(any(call[0] == "turn/start" for call in client.calls))

    def test_needs_user_only_after_terminal_repair_turn_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner, run_id = write_terminal_run(project)
            client = UnrecoverableGoalClient()
            client.goal["status"] = "paused"
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            )
            supervisor._finish_experiment(
                client,
                "thread-goal",
                run_id,
                runner.get_result(run_id).to_dict(),
            )

            state = json.loads((supervisor_dir(project) / "state.json").read_text())
            self.assertEqual(state["state"], "NEEDS_USER")
            self.assertIn("usage limit", state["recovery_error"])

    def test_terminal_blocked_goal_is_delivered_without_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner, run_id = write_terminal_run(project)
            client = RefusingGoalActivationClient()
            client.goal["status"] = "blocked"
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            )

            supervisor._finish_experiment(
                client,
                "thread-goal",
                run_id,
                runner.get_result(run_id).to_dict(),
            )

            state = json.loads((supervisor_dir(project) / "state.json").read_text())
            self.assertEqual(state["state"], "OPEN")
            self.assertNotIn(("goal", "thread-goal", "active"), client.calls)
            self.assertFalse(any(call[0] == "turn/start" for call in client.calls))
            self.assertTrue(client.injected)
            self.assertFalse(list_active_runs(thread_state_root(project, "thread-goal")))
    def test_app_server_starts_and_waits_for_exact_turn(self):
        stdin = io.StringIO()
        stdout = io.StringIO(
            json.dumps(
                {
                    "id": 1,
                    "result": {"turn": {"id": "turn-1", "status": "inProgress"}},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1", "status": "completed"},
                    },
                }
            )
            + "\n"
        )
        client = AppServerClient.__new__(AppServerClient)
        client.cwd = "/tmp/project"
        client.config = type("Config", (), {"app_server_response_timeout_s": 60.0})()
        client._next_id = 1
        client._pending = deque()
        client._stderr = deque()
        client.process = type("Process", (), {"stdin": stdin, "stdout": stdout})()

        turn = client.start_turn("thread-1", "continue")
        completed = client.wait_turn("thread-1", turn["id"], timeout_s=1)

        request = json.loads(stdin.getvalue())
        self.assertEqual(request["method"], "turn/start")
        self.assertEqual(completed["status"], "completed")

    def test_native_goal_runtime_owns_turn_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)

            def complete_goal(client, turn_count):
                client.goal["status"] = "complete"

            client = FakeGoalClient(on_turn_completed=complete_goal)
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            )
            result = supervisor.run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertIn(
                (
                    "resume",
                    "thread-goal",
                    "gpt-5.6-terra",
                    "never",
                    "workspace-write",
                ),
                client.calls,
            )
            self.assertIn(("goal", "thread-goal", "active"), client.calls)
            self.assertFalse(any(call[0] == "turn/start" for call in client.calls))
            with self.assertRaisesRegex(SupervisorError, "operator-paused"):
                supervisor.resume()

    def test_dedicated_supervisor_ignores_foreign_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner, run_id = write_terminal_run(project, thread_id="desktop-thread")
            def complete_goal(client, turn_count):
                client.goal["status"] = "complete"

            client = FakeGoalClient(on_turn_completed=complete_goal)
            result = GoalRuntimeSupervisor(
                project,
                thread_id="supervisor-thread",
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("supervisor-thread"),
                runner=runner,
            ).run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertFalse(client.injected)
            self.assertEqual(
                list_active_runs(thread_state_root(project, "desktop-thread"))[0]["run_id"], run_id
            )
            self.assertFalse(
                any(
                    call[:2] == ("goal", "desktop-thread")
                    for call in client.calls
                )
            )

    def test_experiment_terminal_reactivates_native_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner, run_id = write_terminal_run(project)
            remove_active_run(thread_state_root(project, "thread-goal"), run_id)

            def finish_turn(client, turn_count):
                if turn_count == 1:
                    client.goal["status"] = "paused"
                    register_run(project, run_id)
                else:
                    client.goal["status"] = "complete"

            client = FakeGoalClient(on_turn_completed=finish_turn)
            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            ).run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(client.turn_count, 2)
            self.assertTrue(client.injected)
            self.assertIn(
                '"status": "COMPLETED"',
                client.injected[0]["content"][0]["text"],
            )
            self.assertFalse(list_active_runs(thread_state_root(project, "thread-goal")))

    def test_terminal_wake_waits_for_turn_when_goal_read_is_still_paused(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner, run_id = write_terminal_run(project)
            remove_active_run(thread_state_root(project, "thread-goal"), run_id)

            def finish_turn(client, turn_count):
                if turn_count == 1:
                    client.goal["status"] = "paused"
                    register_run(project, run_id)
                else:
                    client.goal["status"] = "complete"

            client = PersistedPausedActivationClient(on_turn_completed=finish_turn)
            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            ).run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(client.turn_count, 2)
            self.assertTrue(client.injected)
            self.assertFalse(list_active_runs(thread_state_root(project, "thread-goal")))

    def test_running_experiment_allows_multiple_goal_continuations(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner = ExperimentRunner(thread_state_root(project, "thread-goal") / "runs")
            run_id = runner.submit(
                "idea",
                project,
                "python train.py",
                60,
                codex_thread_id="thread-goal",
                launch_worker=False,
            )
            run_dir = runner.runs_dir / run_id
            run = runner.get_run(run_id)
            write_json_atomic(
                run_dir / "run.json",
                {
                    **run,
                    "status": "RUNNING",
                    "worker_pid": os.getpid(),
                    "worker_pid_start_ticks": process_start_ticks(os.getpid()),
                },
            )
            register_run(project, run_id)

            def advance(client, turn_count):
                if turn_count == 2:
                    current = runner.get_run(run_id)
                    finalize_run(
                        run_dir,
                        "completed.json",
                        {
                            "event": "RUN_COMPLETED",
                            "run_id": run_id,
                            "idea_id": "idea",
                            "status": "COMPLETED",
                            "return_code": 0,
                            "finished_at": time.time(),
                        },
                        current,
                    )
                elif turn_count == 3:
                    client.goal["status"] = "complete"

            client = FakeGoalClient(on_turn_completed=advance)
            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            ).run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(client.turn_count, 3)
            self.assertTrue(client.injected)

    def test_running_experiment_still_honors_goal_stop_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner = ExperimentRunner(thread_state_root(project, "thread-goal") / "runs")
            run_id = runner.submit(
                "idea",
                project,
                "python train.py",
                60,
                codex_thread_id="thread-goal",
                launch_worker=False,
            )
            run_dir = runner.runs_dir / run_id
            run = runner.get_run(run_id)
            write_json_atomic(
                run_dir / "run.json",
                {
                    **run,
                    "status": "RUNNING",
                    "worker_pid": os.getpid(),
                    "worker_pid_start_ticks": process_start_ticks(os.getpid()),
                },
            )
            register_run(project, run_id)

            def block_goal(client, turn_count):
                if turn_count == 1:
                    current = runner.get_run(run_id)
                    finalize_run(
                        run_dir,
                        "failed.json",
                        {
                            "event": "RUN_FAILED",
                            "run_id": run_id,
                            "idea_id": "idea",
                            "status": "FAILED",
                            "return_code": 1,
                            "finished_at": time.time(),
                        },
                        current,
                    )
                    client.goal["status"] = "blocked"
                else:
                    client.goal["status"] = "complete"

            client = FakeGoalClient(on_turn_completed=block_goal)
            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            ).run()

            self.assertEqual(result["state"], "OPEN")
            self.assertEqual(client.turn_count, 1)
            self.assertTrue(client.injected)
            self.assertFalse(list_active_runs(thread_state_root(project, "thread-goal")))
            self.assertEqual(
                client.calls.count(("goal", "thread-goal", "active")), 1
            )

    def test_supervisor_owned_experiment_is_submitted_without_worker_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_json_atomic(
                supervisor_dir(project) / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-goal",
                    "state": "OPEN",
                },
            )
            service = ExperimentService(project, thread_id="thread-goal", ensure_supervisor=False)
            with (
                patch.dict("os.environ", {"CODEX_THREAD_ID": "thread-goal"}),
                patch.object(
                    service.runner, "submit", return_value="run-test"
                ) as submit,
            ):
                result = service.submit_experiment(
                    "idea", ".", "python train.py", thread_id="thread-goal"
                )

            self.assertEqual(result["status"], "SUBMITTED")
            self.assertEqual(result["scheduler"], "app_server_supervisor")
            self.assertEqual(result["worker_owner"], "supervisor")
            self.assertEqual(result["goal_pause"]["status"], "NOT_REQUESTED")
            self.assertTrue(result["goal_pause"]["continuation_allowed"])
            self.assertFalse(submit.call_args.kwargs["launch_worker"])
            marker = list_active_runs(thread_state_root(project, "thread-goal"))[0]
            self.assertEqual(
                marker, {"run_id": "run-test", "thread_id": "thread-goal"}
            )

    def test_submission_registry_allows_multiple_active_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_json_atomic(
                supervisor_dir(project) / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-goal",
                    "state": "OPEN",
                },
            )
            service = ExperimentService(project, thread_id="thread-goal", ensure_supervisor=False)
            with (
                patch.dict("os.environ", {"CODEX_THREAD_ID": "thread-goal"}),
                patch.object(
                    service.runner,
                    "submit",
                    side_effect=["run-first", "run-second"],
                ),
            ):
                service.submit_experiment("first", ".", "python first.py")
                service.submit_experiment("second", ".", "python second.py")

            self.assertEqual(
                [item["run_id"] for item in list_active_runs(thread_state_root(project, "thread-goal"))],
                ["run-first", "run-second"],
            )

    def test_supervisor_launches_worker_for_submitted_experiment(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_json_atomic(
                supervisor_dir(project) / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-goal",
                    "state": "OPEN",
                },
            )
            service = ExperimentService(project, thread_id="thread-goal", ensure_supervisor=False)
            code = (
                "import json,os,pathlib; "
                "pathlib.Path(os.environ['AUTO_RESEARCH_RUN_DIR'],'metrics.json')"
                ".write_text(json.dumps({'score': 1.0}))"
            )
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
            with patch.dict("os.environ", {"CODEX_THREAD_ID": ""}):
                submitted = service.submit_experiment(
                    "idea", ".", command, 30, thread_id="thread-goal"
                )
            run_id = submitted["run_id"]
            self.assertEqual(service.runner.get_run(run_id)["status"], "SUBMITTED")
            self.assertIsNone(service.runner.get_run(run_id).get("worker_pid"))

            marker = list_active_runs(thread_state_root(project, "thread-goal"))[0]
            supervisor = GoalRuntimeSupervisor(
                project,
                client_factory=FakeGoalClient,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=service.runner,
            )
            outcome = supervisor._launch_or_observe_experiment(
                FakeGoalClient(), "thread-goal", marker
            )

            self.assertIn(outcome, {"RUNNING", "TERMINAL"})
            terminal = service.runner.wait(run_id, poll_s=0.02)
            self.assertEqual(terminal.status, "COMPLETED")
            self.assertIsNotNone(service.runner.get_run(run_id).get("worker_pid"))

    def test_result_query_does_not_pause_or_request_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            service = ExperimentService(project, thread_id="thread-goal", ensure_supervisor=False)
            run_id = service.runner.submit(
                "idea",
                project,
                "python train.py",
                30,
                codex_thread_id="thread-goal",
                launch_worker=False,
            )
            register_run(project, run_id)

            result = service.get_experiment_result(run_id)

            self.assertEqual(result["status"], "SUBMITTED")
            marker = list_active_runs(thread_state_root(project, "thread-goal"))[0]
            self.assertEqual(
                marker, {"run_id": run_id, "thread_id": "thread-goal"}
            )

    def test_terminal_result_query_and_cancel_do_not_consume_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            service = ExperimentService(project, thread_id="thread-goal", ensure_supervisor=False)
            run_id = service.runner.submit(
                "idea",
                project,
                "python train.py",
                30,
                codex_thread_id="thread-goal",
                launch_worker=False,
            )
            register_run(project, run_id)

            cancelled = service.cancel_experiment(run_id)
            queried = service.get_experiment_result(run_id)

            self.assertEqual(cancelled["status"], "CANCELLED")
            self.assertEqual(queried["status"], "CANCELLED")
            self.assertEqual(
                list_active_runs(thread_state_root(project, "thread-goal"))[0]["run_id"], run_id
            )


if __name__ == "__main__":
    unittest.main()

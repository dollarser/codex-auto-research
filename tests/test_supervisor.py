from __future__ import annotations

import io
import json
import shlex
import sys
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from auto_research.app_server import AppServerClient, AppServerError, AppServerTimeout
from auto_research.ledger import write_json_atomic
from auto_research.mcp_server import ExperimentService
from auto_research.runner import ExperimentRunner, finalize_run
from auto_research.supervisor import (
    GoalRuntimeSupervisor,
    SupervisorError,
    pause_goal_for_experiment,
    resolve_supervisor_session_mode,
    supervisor_active_experiment_path,
    supervisor_dir,
)


def write_goal(project: Path) -> None:
    write_json_atomic(
        project / "goal.json",
        {
            "goal_id": "goal-test",
            "statement": "improve score",
            "primary_metric": "score",
            "hard_requirements": [],
        },
    )


def write_terminal_run(
    project: Path, *, thread_id: str = "thread-goal"
) -> tuple[ExperimentRunner, str]:
    runner = ExperimentRunner(project / "research" / "runs")
    run_id = "run-native-goal-test"
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
    return runner, run_id


class FakeSession:
    def __init__(self, thread_id: str):
        self.thread_id = thread_id

    def prepare(self, *, create_thread: bool):
        assert create_thread
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


class SupervisorTests(unittest.TestCase):
    def test_state_write_clears_phase_local_v3_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_json_atomic(
                supervisor_dir(project) / "state.json",
                {
                    "schema_version": 3,
                    "thread_id": "thread-goal",
                    "state": "FOREIGN_EXPERIMENT_WAITING",
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

            state = supervisor._write_state(
                state="EXPERIMENT_WAITING", run_id="run-current"
            )

            self.assertEqual(state["run_id"], "run-current")
            self.assertNotIn("active_turn_id", state)
            self.assertNotIn("foreign_thread_id", state)
            persisted = json.loads(
                (supervisor_dir(project) / "state.json").read_text()
            )
            self.assertEqual(persisted["schema_version"], 3)

            state = supervisor._write_state(state="GOAL_ACTIVE")
            self.assertNotIn("run_id", state)

    def test_completed_controller_does_not_implicitly_reprepare_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_json_atomic(
                supervisor_dir(project) / "state.json",
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
                session_factory=unexpected_session_prepare,
            ).run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(result["thread_id"], "thread-completed")

    def test_namespaced_bootstrap_ignores_default_active_marker(self):
        """A fresh controller must never adopt a run from the default namespace."""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            namespace = "research/supervisors/fresh-controller"
            write_json_atomic(
                project / "research" / "active_experiment.json",
                {"run_id": "run-from-default-controller", "thread_id": "other"},
            )
            client = FakeGoalClient()
            client.goal["status"] = "complete"
            result = GoalRuntimeSupervisor(
                project,
                state_root=namespace,
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
            self.assertIsNone(state["recovery_turn_id"])
            self.assertFalse(any(call[0] == "goal" for call in client.calls))

    def test_submitted_run_launches_while_goal_turn_is_still_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner = ExperimentRunner(project / "research" / "runs")
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
                    "state": "GOAL_RUNNING",
                },
            )
            write_json_atomic(
                supervisor_active_experiment_path(project),
                {"run_id": run_id, "thread_id": "thread-goal"},
            )
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

    def test_blocked_goal_is_reactivated_without_needs_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)

            def complete_goal(client, _turn_count):
                client.goal["status"] = "complete"

            client = FakeGoalClient(on_turn_completed=complete_goal)
            client.goal["status"] = "blocked"
            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            ).run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertIn(("goal", "thread-goal", "active"), client.calls)

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

    def test_needs_user_only_after_repair_turn_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = UnrecoverableGoalClient()
            client.goal["status"] = "blocked"
            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
            ).run()

            self.assertEqual(result["state"], "NEEDS_USER")
            self.assertIn("usage limit", result["recovery_error"])

    def test_terminal_blocked_goal_uses_repair_turn_when_activation_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner, run_id = write_terminal_run(project)
            write_json_atomic(
                supervisor_active_experiment_path(project),
                {
                    "run_id": run_id,
                    "thread_id": "thread-goal",
                    "wait_requested": False,
                },
            )
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
            self.assertEqual(state["fallback_turn_id"], "fallback-repair-turn")
            self.assertIn("status='blocked'", state["goal_wake_error"])
            self.assertEqual(state["last_terminal_run_id"], run_id)
            self.assertTrue(client.injected)
            self.assertFalse(supervisor_active_experiment_path(project).exists())
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

    def test_supervisor_session_mode_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            dedicated = GoalRuntimeSupervisor(project).session_factory()
            adopted = GoalRuntimeSupervisor(
                project, session_mode="adopted"
            ).session_factory()

            self.assertEqual(dedicated.state_path.name, "supervisor_session.json")
            self.assertEqual(adopted.state_path.name, "codex_session.json")

    def test_auto_session_mode_adopts_a_precreated_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            root = project / "research" / "supervisors" / "new-cycle"
            root.mkdir(parents=True)
            write_json_atomic(root / "codex_session.json", {"thread_id": "new-thread"})

            mode = resolve_supervisor_session_mode(project, state_root=root)
            session = GoalRuntimeSupervisor(project, state_root=root).session_factory()

            self.assertEqual(mode, "adopted")
            self.assertEqual(session.state_path.name, "codex_session.json")

    def test_auto_session_mode_reuses_a_dedicated_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            root = project / "research" / "supervisors" / "existing-cycle"
            root.mkdir(parents=True)
            write_json_atomic(
                root / "supervisor_session.json", {"thread_id": "dedicated-thread"}
            )

            self.assertEqual(
                resolve_supervisor_session_mode(project, state_root=root),
                "dedicated",
            )

    def test_auto_session_mode_rejects_ambiguous_unowned_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            root = project / "research"
            root.mkdir(parents=True)
            write_json_atomic(root / "codex_session.json", {"thread_id": "one"})
            write_json_atomic(root / "supervisor_session.json", {"thread_id": "two"})

            with self.assertRaisesRegex(SupervisorError, "both codex_session"):
                resolve_supervisor_session_mode(project, state_root=root)

            self.assertEqual(
                resolve_supervisor_session_mode(
                    project, state_root=root, session_mode="dedicated"
                ),
                "dedicated",
            )

    def test_auto_session_mode_uses_persisted_controller_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            root = project / "research"
            (root / "supervisor").mkdir(parents=True)
            write_json_atomic(root / "codex_session.json", {"thread_id": "one"})
            write_json_atomic(root / "supervisor_session.json", {"thread_id": "two"})
            write_json_atomic(
                root / "supervisor" / "state.json",
                {
                    "schema_version": 3,
                    "session_mode": "adopted",
                    "thread_id": "one",
                },
            )

            self.assertEqual(
                resolve_supervisor_session_mode(project, state_root=root),
                "adopted",
            )


    def test_dedicated_supervisor_waits_for_foreign_run_without_takeover(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner, run_id = write_terminal_run(project, thread_id="desktop-thread")
            write_json_atomic(
                supervisor_active_experiment_path(project),
                {"run_id": run_id},
            )

            def complete_goal(client, turn_count):
                client.goal["status"] = "complete"

            client = FakeGoalClient(on_turn_completed=complete_goal)
            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("supervisor-thread"),
                runner=runner,
            ).run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(
                result["last_foreign_run"],
                {"run_id": run_id, "thread_id": "desktop-thread"},
            )
            self.assertNotIn("foreign_thread_id", result)
            self.assertTrue(client.injected)
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

            def finish_turn(client, turn_count):
                if turn_count == 1:
                    client.goal["status"] = "paused"
                    write_json_atomic(
                        supervisor_active_experiment_path(project),
                        {"run_id": run_id},
                    )
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
            self.assertFalse(
                supervisor_active_experiment_path(project).exists()
            )

    def test_pause_goal_handoff_uses_managed_goal_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_json_atomic(
                supervisor_dir(project) / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-goal",
                    "state": "GOAL_RUNNING",
                },
            )
            client = FakeGoalClient()
            write_json_atomic(
                supervisor_active_experiment_path(project),
                {
                    "run_id": "run-one",
                    "thread_id": "thread-goal",
                    "wait_requested": False,
                },
            )

            result = pause_goal_for_experiment(
                project,
                thread_id="thread-goal",
                run_id="run-one",
                client_factory=lambda: client,
            )

            self.assertEqual(result["goal_status"], "paused")
            self.assertIn(("goal", "thread-goal", "paused"), client.calls)
            self.assertTrue(
                (supervisor_dir(project) / "experiment_handoff.json").exists()
            )

    def test_running_experiment_allows_multiple_goal_continuations(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner = ExperimentRunner(project / "research" / "runs")
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
            write_json_atomic(run_dir / "run.json", {**run, "status": "RUNNING"})
            write_json_atomic(
                supervisor_active_experiment_path(project),
                {
                    "run_id": run_id,
                    "thread_id": "thread-goal",
                    "wait_requested": False,
                },
            )

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
            runner = ExperimentRunner(project / "research" / "runs")
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
            write_json_atomic(run_dir / "run.json", {**run, "status": "RUNNING"})
            write_json_atomic(
                supervisor_active_experiment_path(project),
                {
                    "run_id": run_id,
                    "thread_id": "thread-goal",
                    "wait_requested": False,
                },
            )

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

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(client.turn_count, 2)
            self.assertTrue(client.injected)
            self.assertFalse(supervisor_active_experiment_path(project).exists())
            self.assertIn(("goal", "thread-goal", "active"), client.calls)

    def test_supervisor_owned_experiment_is_submitted_without_worker_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_json_atomic(
                project / "research" / "codex_session.json",
                {
                    "schema_version": 1,
                    "project_root": str(project.resolve()),
                    "thread_id": "desktop-thread",
                    "setup_state": "ready",
                },
            )
            write_json_atomic(
                supervisor_dir(project) / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-goal",
                    "state": "GOAL_RUNNING",
                },
            )
            service = ExperimentService(project)
            write_json_atomic(
                project / "research" / "active_experiment.json",
                {"run_id": "foreign-desktop-run"},
            )
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
            self.assertFalse(submit.call_args.kwargs["wake_enabled"])
            self.assertFalse(submit.call_args.kwargs["launch_worker"])
            marker = json.loads(
                supervisor_active_experiment_path(project).read_text()
            )
            self.assertEqual(marker["run_id"], "run-test")
            self.assertFalse(marker["wait_requested"])

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
                    "state": "GOAL_RUNNING",
                },
            )
            service = ExperimentService(project)
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

            marker = json.loads(
                supervisor_active_experiment_path(project).read_text(encoding="utf-8")
            )
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

    def test_explicit_wait_handoff_pauses_supervisor_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_json_atomic(
                supervisor_dir(project) / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-goal",
                    "state": "GOAL_RUNNING",
                },
            )
            service = ExperimentService(project)
            runner = ExperimentRunner(project / "research" / "runs")
            run_id = runner.submit(
                "idea",
                project,
                "python train.py",
                30,
                codex_thread_id="thread-goal",
                launch_worker=False,
            )
            write_json_atomic(
                supervisor_active_experiment_path(project),
                {
                    "run_id": run_id,
                    "thread_id": "thread-goal",
                    "wait_requested": False,
                },
            )
            client = FakeGoalClient()
            with (
                patch.dict("os.environ", {"CODEX_THREAD_ID": "thread-goal"}),
                patch(
                    "auto_research.mcp_server.pause_goal_for_experiment",
                    side_effect=lambda *args, **kwargs: pause_goal_for_experiment(
                        *args, **kwargs, client_factory=lambda: client
                    ),
                ),
            ):
                result = service.wait_for_experiment(run_id)

            self.assertEqual(result["wait_handoff"], "PAUSED")
            self.assertIn(("goal", "thread-goal", "paused"), client.calls)
            marker = json.loads(
                supervisor_active_experiment_path(project).read_text()
            )
            self.assertTrue(marker["wait_requested"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from auto_research.app_server import AppServerClient
from auto_research.ledger import write_json_atomic
from auto_research.mcp_server import ExperimentService
from auto_research.runner import ExperimentRunner, finalize_run
from auto_research.supervisor import (
    GoalRuntimeSupervisor,
    SupervisorError,
    pause_goal_for_experiment,
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

    def resume_thread(self, thread_id):
        self.calls.append(("resume", thread_id))
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

    def wait_turn(self, thread_id, turn_id):
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


class SupervisorTests(unittest.TestCase):
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
            self.assertIn(("goal", "thread-goal", "active"), client.calls)
            self.assertFalse(any(call[0] == "turn/start" for call in client.calls))
            with self.assertRaisesRegex(SupervisorError, "operator-paused"):
                supervisor.resume()

    def test_supervisor_session_mode_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            dedicated = GoalRuntimeSupervisor(project).session_factory()
            adopted = GoalRuntimeSupervisor(
                project, adopt_session=True
            ).session_factory()

            self.assertEqual(dedicated.state_path.name, "supervisor_session.json")
            self.assertEqual(adopted.state_path.name, "codex_session.json")

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
            self.assertEqual(result["foreign_thread_id"], "desktop-thread")
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
                    "schema_version": 2,
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
                client.goal["status"] = "blocked"

            client = FakeGoalClient(on_turn_completed=block_goal)
            result = GoalRuntimeSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-goal"),
                runner=runner,
            ).run()

            self.assertEqual(result["state"], "NEEDS_USER")
            self.assertEqual(result["goal"]["status"], "blocked")
            self.assertEqual(client.turn_count, 1)

    def test_supervisor_owned_experiment_starts_without_pausing_goal(self):
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
                    "schema_version": 2,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-goal",
                    "state": "GOAL_RUNNING",
                },
            )
            launches = []
            service = ExperimentService(
                project,
                wake_launcher=lambda *args, **kwargs: launches.append(args),
            )
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
                result = service.start_experiment(
                    "idea", ".", "python train.py", thread_id="thread-goal"
                )

            self.assertEqual(result["scheduler"], "app_server_goal_runtime")
            self.assertEqual(result["goal_pause"]["status"], "NOT_REQUESTED")
            self.assertTrue(result["goal_pause"]["continuation_allowed"])
            self.assertEqual(result["wake_listener"]["status"], "DISABLED")
            self.assertEqual(launches, [])
            self.assertFalse(submit.call_args.kwargs["wake_enabled"])
            self.assertTrue(submit.call_args.kwargs["launch_worker"])
            marker = json.loads(
                supervisor_active_experiment_path(project).read_text()
            )
            self.assertEqual(marker["run_id"], "run-test")
            self.assertFalse(marker["wait_requested"])

    def test_explicit_wait_handoff_pauses_supervisor_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_json_atomic(
                supervisor_dir(project) / "state.json",
                {
                    "schema_version": 2,
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

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_research.app_server import AppServerClient
from auto_research.ledger import write_json_atomic
from auto_research.mcp_server import ExperimentService
from auto_research.runner import ExperimentRunner, finalize_run
from auto_research.supervisor import (
    AppServerSupervisor,
    SupervisorError,
    submit_handoff,
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


class FakeSession:
    def __init__(self, thread_id: str):
        self.thread_id = thread_id

    def prepare(self, *, create_thread: bool):
        assert create_thread
        return {"thread_id": self.thread_id}


class FakeTurnClient:
    def __init__(self, on_wait):
        self.on_wait = on_wait
        self.prompts: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def initialize(self):
        return None

    def resume_thread(self, thread_id):
        return {"id": thread_id}

    def start_turn(self, thread_id, text):
        self.prompts.append(text)
        return {"id": f"turn-{len(self.prompts)}", "status": "inProgress"}

    def wait_turn(self, thread_id, turn_id):
        self.on_wait()
        return {"id": turn_id, "status": "completed"}


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
        from collections import deque

        client._pending = deque()
        client._stderr = deque()
        client.process = type("Process", (), {"stdin": stdin, "stdout": stdout})()

        turn = client.start_turn("thread-1", "continue")
        completed = client.wait_turn("thread-1", turn["id"], timeout_s=1)

        request = json.loads(stdin.getvalue())
        self.assertEqual(request["method"], "turn/start")
        self.assertEqual(request["params"]["threadId"], "thread-1")
        self.assertEqual(completed["status"], "completed")

    def test_complete_handoff_ends_goal_without_goal_state_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client: FakeTurnClient

            def handoff():
                active = json.loads(
                    (supervisor_dir(project) / "active_turn.json").read_text()
                )
                submit_handoff(
                    project,
                    turn_attempt_id=active["turn_attempt_id"],
                    action="COMPLETE",
                    summary="target reached",
                )

            client = FakeTurnClient(handoff)
            supervisor = AppServerSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-supervisor"),
            )
            result = supervisor.run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(result["last_handoff"]["summary"], "target reached")
            self.assertIn("submit_supervisor_handoff", client.prompts[0])
            with self.assertRaisesRegex(SupervisorError, "operator-paused"):
                supervisor.resume()

    def test_wait_handoff_consumes_terminal_event_then_starts_analysis_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            runner = ExperimentRunner(project / "research" / "runs")
            run_id = "run-wait-test"
            run_dir = runner.runs_dir / run_id
            (run_dir / "events").mkdir(parents=True)
            run = {
                "run_id": run_id,
                "idea_id": "wait",
                "worktree": str(project),
                "command": "python train.py",
                "argv": ["python", "train.py"],
                "timeout_s": 60,
                "created_at": time.time(),
                "status": "RUNNING",
                "codex_thread_id": "thread-supervisor",
            }
            write_json_atomic(run_dir / "run.json", run)
            finalize_run(
                run_dir,
                "completed.json",
                {
                    "event": "RUN_COMPLETED",
                    "run_id": run_id,
                    "idea_id": "wait",
                    "status": "COMPLETED",
                    "return_code": 0,
                    "finished_at": time.time(),
                },
                run,
            )
            calls = 0

            def handoff():
                nonlocal calls
                calls += 1
                active = json.loads(
                    (supervisor_dir(project) / "active_turn.json").read_text()
                )
                submit_handoff(
                    project,
                    turn_attempt_id=active["turn_attempt_id"],
                    action="WAIT_FOR_RUN" if calls == 1 else "COMPLETE",
                    run_id=run_id if calls == 1 else None,
                )

            client = FakeTurnClient(handoff)
            result = AppServerSupervisor(
                project,
                client_factory=lambda: client,
                session_factory=lambda: FakeSession("thread-supervisor"),
                runner=runner,
            ).run()

            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(len(client.prompts), 2)
            self.assertIn('"status": "COMPLETED"', client.prompts[1])

    def test_supervisor_owned_experiment_does_not_launch_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_json_atomic(
                supervisor_dir(project) / "state.json",
                {
                    "schema_version": 1,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-supervisor",
                    "state": "TURN_RUNNING",
                },
            )
            launches = []
            service = ExperimentService(
                project,
                wake_launcher=lambda *args, **kwargs: launches.append(args),
            )
            with (
                patch.dict("os.environ", {"CODEX_THREAD_ID": "thread-supervisor"}),
                patch.object(
                    service.runner, "submit", return_value="run-test"
                ) as submit,
            ):
                result = service.start_experiment(
                    "idea", ".", "python train.py", thread_id="thread-supervisor"
                )

            self.assertEqual(result["scheduler"], "app_server_supervisor")
            self.assertEqual(result["wake_listener"]["status"], "DISABLED")
            self.assertEqual(launches, [])
            self.assertFalse(submit.call_args.kwargs["wake_enabled"])


if __name__ == "__main__":
    unittest.main()

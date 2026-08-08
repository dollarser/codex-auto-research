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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_research.app_server import AppServerClient
from auto_research.cli import main as cli_main
from auto_research.config import load_config
from auto_research.ledger import write_json_atomic
from auto_research.mcp_config import register_mcp_config, render_mcp_config
from auto_research.mcp_server import ExperimentService
from auto_research.runner import ExperimentRunner, finalize_run
from auto_research.wake_listener import (
    GoalBindingError,
    GoalWakeListener,
    recover_wake_listeners,
)


def write_goal(project: Path) -> None:
    write_json_atomic(
        project / "goal.json",
        {
            "goal_id": "goal-test",
            "statement": "maximize score",
            "primary_metric": "score",
            "direction": "maximize",
            "search_space": {"editable_paths": ["src"], "sealed_paths": ["eval"]},
            "constraints": {"max_wall_time_s": 60},
            "hard_requirements": [],
            "stopping": {"max_experiments": 5},
        },
    )


def write_terminal_run(project: Path, run_id: str = "run-test-001") -> Path:
    run_dir = project / "research" / "runs" / run_id
    (run_dir / "events").mkdir(parents=True)
    run = {
        "run_id": run_id,
        "idea_id": "idea-test",
        "worktree": str(project),
        "command": "python train.py",
        "argv": ["python", "train.py"],
        "timeout_s": 60,
        "created_at": time.time(),
        "status": "RUNNING",
    }
    write_json_atomic(run_dir / "run.json", run)
    write_json_atomic(run_dir / "metrics.json", {"score": 0.9})
    finalize_run(
        run_dir,
        "completed.json",
        {
            "event": "RUN_COMPLETED",
            "run_id": run_id,
            "idea_id": "idea-test",
            "status": "COMPLETED",
            "return_code": 0,
            "finished_at": time.time(),
        },
        run,
    )
    return run_dir


class FakeAppServer:
    def __init__(
        self,
        *,
        goal_status: str = "paused",
        thread_status: str = "idle",
        threads: list[dict] | None = None,
        goals: dict[str, dict] | None = None,
    ):
        self.goal_status = goal_status
        self.thread_status = thread_status
        self.threads = threads or []
        self.goals = goals or {}
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def initialize(self):
        self.calls.append(("initialize",))

    def close(self):
        self.calls.append(("close",))

    def list_threads(self):
        return self.threads

    def get_goal(self, thread_id):
        return self.goals.get(
            thread_id, {"threadId": thread_id, "status": self.goal_status}
        )

    def resume_thread(self, thread_id):
        self.calls.append(("resume", thread_id))
        return {"id": thread_id, "status": {"type": self.thread_status}}

    def read_thread(self, thread_id):
        return {"id": thread_id, "status": {"type": self.thread_status}}

    def set_goal_status(self, thread_id, status):
        self.calls.append(("goal", thread_id, status))
        self.goal_status = status
        return {"threadId": thread_id, "status": status}

    def start_turn(self, thread_id, prompt):
        self.calls.append(("turn", thread_id, prompt))
        return "turn-wake-1"

    def wait_until_goal_quiescent(self, thread_id, turn_id):
        self.calls.append(("wait", thread_id, turn_id))
        return "paused"


class AgentTests(unittest.TestCase):
    def test_cli_start_does_not_confuse_subcommand_with_experiment_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            (project / "research").mkdir(exist_ok=True)
            (project / "research" / "config.toml").write_text(
                "[listener]\nauto_wake=false\n",
                encoding="utf-8",
            )
            code = (
                "import json,os,pathlib; "
                "pathlib.Path(os.environ['AUTO_RESEARCH_RUN_DIR'],'metrics.json')"
                ".write_text(json.dumps({'score': 1.0}))"
            )
            output = io.StringIO()
            previous = sys.stdout
            sys.stdout = output
            try:
                exit_code = cli_main(
                    [
                        "start",
                        "--project",
                        str(project),
                        "--idea-id",
                        "cli-smoke",
                        "--worktree",
                        str(project),
                        "--command",
                        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
                    ]
                )
            finally:
                sys.stdout = previous
            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "RUNNING")
            result = ExperimentRunner(project / "research" / "runs").wait(
                payload["run_id"], poll_s=0.02
            )
            self.assertEqual(result.status, "COMPLETED")

    def test_config_file_and_environment_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "research").mkdir()
            (root / "research" / "config.toml").write_text(
                "[codex]\nmodel='file-model'\nreasoning_effort='low'\n"
                "[listener]\nauto_wake=false\nreconnect_max_s=12\n"
                "[experiment]\nuse_shell=false\n",
                encoding="utf-8",
            )
            previous = os.environ.get("AUTO_RESEARCH_CODEX_MODEL")
            os.environ["AUTO_RESEARCH_CODEX_MODEL"] = "env-model"
            try:
                config = load_config(root)
            finally:
                if previous is None:
                    os.environ.pop("AUTO_RESEARCH_CODEX_MODEL", None)
                else:
                    os.environ["AUTO_RESEARCH_CODEX_MODEL"] = previous
            self.assertEqual(config.codex_model, "env-model")
            self.assertEqual(config.codex_reasoning_effort, "low")
            self.assertFalse(config.auto_wake)
            self.assertFalse(config.use_shell)
            self.assertEqual(config.reconnect_max_s, 12)

    def test_register_mcp_preserves_other_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = project / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_text(
                '[profiles.default]\nmodel = "gpt-test"\n\n'
                '[mcp_servers.experiment]\ncommand = "old"\n\n'
                '[mcp_servers.other]\ncommand = "other"\n',
                encoding="utf-8",
            )
            registered = register_mcp_config(project)
            content = registered.read_text(encoding="utf-8")
            self.assertIn("[mcp_servers.other]", content)
            self.assertIn("auto_research.mcp_server", content)
            self.assertEqual(content.count("[mcp_servers.experiment]"), 1)

    def test_render_mcp_uses_virtualenv_entrypoint(self):
        rendered = render_mcp_config(".", "/tmp/project/.venv/bin/python")
        self.assertIn('command = "/tmp/project/.venv/bin/python"', rendered)

    def test_experiment_service_starts_worker_and_arms_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            launches: list[dict] = []

            def launcher(project_dir, run_id, *, thread_id=None):
                launches.append(
                    {
                        "project": str(project_dir),
                        "run_id": run_id,
                        "thread_id": thread_id,
                    }
                )
                return {"status": "ARMED", "pid": 123, "thread_id": thread_id}

            service = ExperimentService(project, wake_launcher=launcher)
            code = (
                "import json,os,pathlib; "
                "pathlib.Path(os.environ['AUTO_RESEARCH_RUN_DIR'],'metrics.json')"
                ".write_text(json.dumps({'score': 1.0}))"
            )
            result = service.start_experiment(
                "idea-one",
                ".",
                f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
                30,
                thread_id="thread-current",
            )
            terminal = service.runner.wait(result["run_id"], poll_s=0.02)
            self.assertEqual(terminal.status, "COMPLETED")
            self.assertEqual(result["wake_listener"]["status"], "ARMED")
            self.assertEqual(launches[0]["thread_id"], "thread-current")
            run = service.runner.get_run(result["run_id"])
            self.assertEqual(run["runtime_version"], 3)
            self.assertTrue(run["wake_enabled"])
            self.assertEqual(run["codex_thread_id"], "thread-current")

    def test_idempotency_key_returns_same_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            service = ExperimentService(
                project,
                wake_launcher=lambda *args, **kwargs: {"status": "ARMED"},
            )
            code = (
                "import json,os,pathlib; "
                "pathlib.Path(os.environ['AUTO_RESEARCH_RUN_DIR'],'metrics.json')"
                ".write_text(json.dumps({'score': 1.0}))"
            )
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
            first = service.start_experiment(
                "idea-one", ".", command, 30, idempotency_key="same", thread_id="t1"
            )
            service.runner.wait(first["run_id"], poll_s=0.02)
            second = service.start_experiment(
                "idea-one", ".", command, 30, idempotency_key="same", thread_id="t1"
            )
            self.assertEqual(first["run_id"], second["run_id"])

    def test_finalize_run_commits_only_one_terminal_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-test"
            (run_dir / "events").mkdir(parents=True)
            run = {"run_id": "run-test", "idea_id": "idea", "status": "RUNNING"}
            first = finalize_run(
                run_dir,
                "completed.json",
                {"status": "COMPLETED", "run_id": "run-test"},
                run,
            )
            second = finalize_run(
                run_dir,
                "failed.json",
                {"status": "FAILED", "run_id": "run-test"},
                run,
            )
            self.assertTrue(first)
            self.assertFalse(second)

    def test_wake_listener_binds_explicit_thread_and_wakes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_dir = write_terminal_run(project)
            clients: list[FakeAppServer] = []

            def factory():
                client = FakeAppServer(goal_status="paused", thread_status="idle")
                clients.append(client)
                return client

            listener = GoalWakeListener(
                project,
                run_dir.name,
                thread_id="thread-current",
                client_factory=factory,
            )
            state = listener.run()
            self.assertEqual(state["state"], "WOKEN")
            self.assertEqual(state["thread_id"], "thread-current")
            wake_client = clients[-1]
            self.assertIn(("goal", "thread-current", "active"), wake_client.calls)
            turn_calls = [call for call in wake_client.calls if call[0] == "turn"]
            self.assertEqual(len(turn_calls), 1)
            self.assertIn(run_dir.name, turn_calls[0][2])

            # Durable WOKEN state makes a repeated event/recovery a no-op.
            again = listener.run()
            self.assertEqual(again["wake_turn_id"], "turn-wake-1")
            self.assertEqual(len(clients), 2)

    def test_wake_listener_does_not_duplicate_an_active_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_dir = write_terminal_run(project)
            clients: list[FakeAppServer] = []

            def factory():
                client = FakeAppServer(goal_status="active", thread_status="active")
                clients.append(client)
                return client

            state = GoalWakeListener(
                project,
                run_dir.name,
                thread_id="thread-current",
                client_factory=factory,
            ).run()
            self.assertEqual(state["state"], "SKIPPED")
            self.assertFalse(any(call[0] == "turn" for call in clients[-1].calls))

    def test_thread_discovery_prefers_recent_active_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_dir = write_terminal_run(project)
            now = time.time()
            client = FakeAppServer(
                threads=[
                    {"id": "thread-paused", "updatedAt": now - 5},
                    {"id": "thread-active", "updatedAt": now - 10},
                ],
                goals={
                    "thread-paused": {"status": "paused"},
                    "thread-active": {"status": "active"},
                },
            )
            listener = GoalWakeListener(
                project,
                run_dir.name,
                client_factory=lambda: client,
            )
            thread_id = listener._discover_thread(client, now)
            self.assertEqual(thread_id, "thread-active")

    def test_thread_discovery_rejects_ambiguous_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_dir = write_terminal_run(project)
            now = time.time()
            client = FakeAppServer(
                threads=[
                    {"id": "thread-a", "updatedAt": now},
                    {"id": "thread-b", "updatedAt": now},
                ],
                goals={
                    "thread-a": {"status": "paused"},
                    "thread-b": {"status": "paused"},
                },
            )
            listener = GoalWakeListener(
                project, run_dir.name, client_factory=lambda: client
            )
            with self.assertRaises(GoalBindingError):
                listener._discover_thread(client, now)

    def test_recover_wakes_ignores_pre_v3_historical_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_terminal_run(project, "run-historical-001")
            with patch("auto_research.wake_listener.spawn_wake_listener") as spawn:
                recovered = recover_wake_listeners(project)
            self.assertEqual(recovered, [])
            spawn.assert_not_called()

    def test_app_server_list_threads_uses_exact_cwd_filter(self):
        stdin = io.StringIO()
        stream = io.StringIO(
            json.dumps(
                {
                    "id": 1,
                    "result": {"data": [{"id": "thread-1"}], "nextCursor": None},
                }
            )
            + "\n"
        )
        client = AppServerClient.__new__(AppServerClient)
        client.cwd = "/tmp/project"
        client.config = SimpleNamespace(app_server_response_timeout_s=60.0)
        client._next_id = 1
        client._pending = deque()
        client._stderr = deque()
        client.process = SimpleNamespace(stdin=stdin, stdout=stream)
        threads = client.list_threads()
        request = json.loads(stdin.getvalue())
        self.assertEqual(threads, [{"id": "thread-1"}])
        self.assertEqual(request["method"], "thread/list")
        self.assertEqual(request["params"]["cwd"], "/tmp/project")

    def test_app_server_goal_activation_does_not_replace_objective(self):
        stdin = io.StringIO()
        stream = io.StringIO(
            json.dumps({"id": 1, "result": {"goal": {"status": "active"}}}) + "\n"
        )
        client = AppServerClient.__new__(AppServerClient)
        client.cwd = "/tmp/project"
        client.config = SimpleNamespace(app_server_response_timeout_s=60.0)
        client._next_id = 1
        client._pending = deque()
        client._stderr = deque()
        client.process = SimpleNamespace(stdin=stdin, stdout=stream)
        client.set_goal_status("thread-1", "active")
        request = json.loads(stdin.getvalue())
        self.assertEqual(
            request["params"], {"threadId": "thread-1", "status": "active"}
        )
        self.assertNotIn("objective", request["params"])

    def test_app_server_waits_for_final_item_after_goal_pauses(self):
        stream = io.StringIO(
            "\n".join(
                [
                    json.dumps(
                        {
                            "method": "thread/goal/updated",
                            "params": {"goal": {"status": "paused"}},
                        }
                    ),
                    json.dumps(
                        {
                            "method": "item/completed",
                            "params": {
                                "turnId": "turn-1",
                                "item": {
                                    "type": "agentMessage",
                                    "phase": "final_answer",
                                },
                            },
                        }
                    ),
                ]
            )
            + "\n"
        )
        client = AppServerClient.__new__(AppServerClient)
        client.cwd = "/tmp/project"
        client.config = SimpleNamespace(
            app_server_response_timeout_s=60.0,
            resumed_turn_timeout_s=60.0,
        )
        client._next_id = 1
        client._pending = deque()
        client._stderr = deque()
        client.process = SimpleNamespace(stdin=io.StringIO(), stdout=stream)
        self.assertEqual(
            client.wait_until_goal_quiescent("thread-1", "turn-1"),
            "paused",
        )


if __name__ == "__main__":
    unittest.main()

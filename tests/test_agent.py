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

from auto_research.app_server import (
    MANAGED_APP_SERVER_MAX_MESSAGE_BYTES,
    AppServerClient,
)
from auto_research.cli import main as cli_main
from auto_research.config import load_config
from auto_research.ledger import write_json_atomic
from auto_research.mcp_config import register_mcp_config, render_mcp_config
from auto_research.mcp_server import ExperimentService
from auto_research.research_session import (
    ResearchSessionError,
    ResearchSessionManager,
)
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
        "pause_goal_on_turn_end": False,
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
        goal_reads: list[dict] | None = None,
    ):
        self.goal_status = goal_status
        self.thread_status = thread_status
        self.threads = threads or []
        self.goals = goals or {}
        self.goal_reads = deque(goal_reads or [])
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
        if self.goal_reads:
            value = self.goal_reads.popleft()
            self.goal_status = value.get("status", self.goal_status)
            return value
        return self.goals.get(
            thread_id, {"threadId": thread_id, "status": self.goal_status}
        )

    def read_thread(self, thread_id, *, include_turns=False):
        return {"id": thread_id, "status": {"type": self.thread_status}}

    def set_goal_status(self, thread_id, status):
        self.calls.append(("goal", thread_id, status))
        self.goal_status = status
        return {"threadId": thread_id, "status": status}


class FakeSessionAppServer:
    def __init__(self, project: Path):
        self.project = project.resolve()
        self.threads: dict[str, dict] = {}
        self.goals: dict[str, dict] = {}
        self.names: dict[str, str] = {}
        self.start_count = 0
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def initialize(self):
        self.calls.append(("initialize",))

    def start_thread(self, *, service_name):
        self.start_count += 1
        thread_id = f"thread-{self.start_count}"
        thread = {"id": thread_id, "cwd": str(self.project)}
        self.threads[thread_id] = thread
        self.calls.append(("start", service_name, str(self.project)))
        return thread

    def read_thread(self, thread_id):
        return self.threads[thread_id]

    def set_thread_name(self, thread_id, name):
        self.names[thread_id] = name
        self.calls.append(("name", thread_id, name))

    def get_goal(self, thread_id):
        return self.goals.get(thread_id)

    def set_goal(self, thread_id, *, objective=None, status=None):
        goal = dict(self.goals.get(thread_id, {}))
        if objective is not None:
            goal["objective"] = objective
        if status is not None:
            goal["status"] = status
        goal["threadId"] = thread_id
        self.goals[thread_id] = goal
        self.calls.append(("set_goal", thread_id, objective, status))
        return goal


class AgentTests(unittest.TestCase):
    def test_research_session_create_is_idempotent_and_project_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = FakeSessionAppServer(project)
            manager = ResearchSessionManager(
                project, client_factory=lambda: client
            )

            first = manager.prepare(create_thread=True, title="Dedicated Research")
            second = manager.prepare(create_thread=True)

            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["thread_id"], second["thread_id"])
            self.assertEqual(client.start_count, 1)
            self.assertEqual(
                client.threads[first["thread_id"]]["cwd"], str(project.resolve())
            )
            self.assertEqual(client.goals[first["thread_id"]]["status"], "paused")
            state = json.loads(
                (project / "research" / "codex_session.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["setup_state"], "ready")
            self.assertEqual(state["thread_id"], first["thread_id"])

    def test_research_session_can_adopt_an_existing_project_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            client = FakeSessionAppServer(project)
            client.threads["thread-existing"] = {
                "id": "thread-existing",
                "cwd": str(project),
            }
            client.goals["thread-existing"] = {
                "threadId": "thread-existing",
                "objective": "already optimized",
                "status": "paused",
            }

            result = ResearchSessionManager(
                project, client_factory=lambda: client
            ).prepare(thread_id="thread-existing")

            self.assertFalse(result["created"])
            self.assertEqual(result["thread_id"], "thread-existing")
            self.assertEqual(result["objective"], "already optimized")
            self.assertEqual(client.start_count, 0)

    def test_research_session_refuses_corrupt_state_instead_of_creating(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            state_path = project / "research" / "codex_session.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text("{not-json", encoding="utf-8")
            client = FakeSessionAppServer(project)

            with self.assertRaisesRegex(
                ResearchSessionError, "refusing to create a duplicate"
            ):
                ResearchSessionManager(
                    project, client_factory=lambda: client
                ).prepare(create_thread=True)
            self.assertEqual(client.start_count, 0)

    def test_research_session_validates_objective_before_thread_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            project.joinpath("goal.json").write_text("{}", encoding="utf-8")
            client = FakeSessionAppServer(project)

            with self.assertRaisesRegex(ResearchSessionError, "statement"):
                ResearchSessionManager(
                    project, client_factory=lambda: client
                ).prepare(create_thread=True)
            self.assertEqual(client.start_count, 0)
            self.assertFalse(
                (project / "research" / "codex_session.json").exists()
            )

    def test_research_session_rejects_thread_from_another_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            other = Path(tmp) / "other"
            project.mkdir()
            other.mkdir()
            write_goal(project)
            client = FakeSessionAppServer(project)
            client.threads["wrong-thread"] = {
                "id": "wrong-thread",
                "cwd": str(other),
            }

            with self.assertRaisesRegex(ResearchSessionError, "is bound to"):
                ResearchSessionManager(
                    project, client_factory=lambda: client
                ).prepare(thread_id="wrong-thread")

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
                "[listener]\nauto_wake=false\nreconnect_max_s=12\n"
                "[experiment]\nuse_shell=false\n",
                encoding="utf-8",
            )
            previous = os.environ.get("AUTO_RESEARCH_RECONNECT_MAX_S")
            os.environ["AUTO_RESEARCH_RECONNECT_MAX_S"] = "13"
            try:
                config = load_config(root)
            finally:
                if previous is None:
                    os.environ.pop("AUTO_RESEARCH_RECONNECT_MAX_S", None)
                else:
                    os.environ["AUTO_RESEARCH_RECONNECT_MAX_S"] = previous
            self.assertFalse(config.auto_wake)
            self.assertFalse(config.use_shell)
            self.assertEqual(config.reconnect_max_s, 13)

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
            with patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):
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
            self.assertFalse(run["pause_goal_on_turn_end"])

    def test_experiment_started_from_codex_allows_continuations(self):
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
            previous = os.environ.get("CODEX_THREAD_ID")
            os.environ["CODEX_THREAD_ID"] = "thread-current"
            try:
                result = service.start_experiment(
                    "idea-one",
                    ".",
                    f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
                    30,
                )
            finally:
                if previous is None:
                    os.environ.pop("CODEX_THREAD_ID", None)
                else:
                    os.environ["CODEX_THREAD_ID"] = previous
            run = service.runner.get_run(result["run_id"])
            self.assertFalse(run["pause_goal_on_turn_end"])
            self.assertEqual(run["codex_thread_id"], "thread-current")

    def test_experiment_uses_persisted_dedicated_thread_outside_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_json_atomic(
                project / "research" / "codex_session.json",
                {
                    "schema_version": 1,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-dedicated",
                    "setup_state": "ready",
                },
            )
            launches: list[str | None] = []
            service = ExperimentService(
                project,
                wake_launcher=lambda *args, thread_id=None, **kwargs: (
                    launches.append(thread_id)
                    or {"status": "ARMED", "thread_id": thread_id}
                ),
            )
            code = (
                "import json,os,pathlib; "
                "pathlib.Path(os.environ['AUTO_RESEARCH_RUN_DIR'],'metrics.json')"
                ".write_text(json.dumps({'score': 1.0}))"
            )

            with patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):
                result = service.start_experiment(
                    "idea-dedicated",
                    ".",
                    f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
                    30,
                )
            service.runner.wait(result["run_id"], poll_s=0.02)

            self.assertEqual(launches, ["thread-dedicated"])
            run = service.runner.get_run(result["run_id"])
            self.assertEqual(run["codex_thread_id"], "thread-dedicated")
            self.assertFalse(run["pause_goal_on_turn_end"])

    def test_experiment_rejects_current_thread_that_differs_from_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_json_atomic(
                project / "research" / "codex_session.json",
                {
                    "schema_version": 1,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-dedicated",
                    "setup_state": "ready",
                },
            )
            service = ExperimentService(project)
            previous = os.environ.get("CODEX_THREAD_ID")
            os.environ["CODEX_THREAD_ID"] = "thread-other"
            try:
                with self.assertRaisesRegex(ValueError, "dedicated thread"):
                    service.start_experiment(
                        "idea-wrong-thread", ".", "python train.py", 30
                    )
            finally:
                if previous is None:
                    os.environ.pop("CODEX_THREAD_ID", None)
                else:
                    os.environ["CODEX_THREAD_ID"] = previous

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
            with patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):
                first = service.start_experiment(
                    "idea-one",
                    ".",
                    command,
                    30,
                    idempotency_key="same",
                    thread_id="t1",
                )
                service.runner.wait(first["run_id"], poll_s=0.02)
                second = service.start_experiment(
                    "idea-one",
                    ".",
                    command,
                    30,
                    idempotency_key="same",
                    thread_id="t1",
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
            self.assertEqual(state["state"], "ACTIVATED")
            self.assertEqual(state["thread_id"], "thread-current")
            wake_client = clients[-1]
            self.assertIn(("goal", "thread-current", "active"), wake_client.calls)
            self.assertFalse(any(call[0] == "resume" for call in wake_client.calls))
            self.assertFalse(any(call[0] == "turn" for call in wake_client.calls))

            # Durable ACTIVATED state makes a repeated event/recovery a no-op.
            again = listener.run()
            self.assertEqual(again["state"], "ACTIVATED")
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
            self.assertFalse(any(call[0] == "goal" for call in clients[-1].calls))

    def test_wake_listener_reactivates_blocked_goal_after_terminal_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_dir = write_terminal_run(project)
            client = FakeAppServer(goal_status="blocked")
            listener = GoalWakeListener(
                project,
                run_dir.name,
                thread_id="thread-current",
                client_factory=lambda: client,
            )

            state = listener.run()

            self.assertEqual(state["state"], "ACTIVATED")
            self.assertIn(("goal", "thread-current", "active"), client.calls)

    def test_listener_treats_blocked_as_managed_experiment_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_dir = write_terminal_run(project)
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            run["pause_goal_on_turn_end"] = True
            write_json_atomic(run_dir / "run.json", run)
            client = FakeAppServer(goal_status="blocked")
            listener = GoalWakeListener(
                project,
                run_dir.name,
                thread_id="thread-current",
                client_factory=lambda: client,
            )

            state = listener._pause_goal_after_current_turn("thread-current")

            self.assertEqual(state["state"], "WAITING")
            self.assertEqual(state["pause_mode"], "blocked_wait")

    def test_listener_recovers_a_persisted_pause_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_dir = write_terminal_run(project)
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            run["pause_goal_on_turn_end"] = True
            write_json_atomic(run_dir / "run.json", run)
            write_json_atomic(
                run_dir / "wake.json",
                {
                    "state": "PAUSE_BOUNDARY_DETECTED",
                    "pause_boundary_detected_at": 123.0,
                    "pause_baseline_tokens": 10,
                },
            )
            client = FakeAppServer(
                goal_reads=[{"status": "paused", "tokensUsed": 20}]
            )
            listener = GoalWakeListener(
                project,
                run_dir.name,
                thread_id="thread-current",
                client_factory=lambda: client,
            )
            state = listener._pause_goal_after_current_turn("thread-current")
            self.assertEqual(state["state"], "WAITING")
            self.assertEqual(state["pause_mode"], "turn_boundary_recovered")
            self.assertEqual(state["pause_boundary_observed_at"], 123.0)

    def test_listener_reasserts_pause_after_submitting_turn_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_dir = write_terminal_run(project)
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            run["pause_goal_on_turn_end"] = True
            write_json_atomic(run_dir / "run.json", run)
            client = FakeAppServer(
                goal_reads=[
                    {"status": "active", "tokensUsed": 10, "updatedAt": 1},
                    {"status": "active", "tokensUsed": 20, "updatedAt": 2},
                ]
            )
            listener = GoalWakeListener(
                project,
                run_dir.name,
                thread_id="thread-current",
                client_factory=lambda: client,
            )
            state = listener._pause_goal_after_current_turn("thread-current")
            self.assertEqual(state["state"], "WAITING")
            self.assertEqual(state["pause_boundary_reason"], "usage_advanced")
            goal_calls = [call for call in client.calls if call[0] == "goal"]
            self.assertEqual(
                goal_calls,
                [
                    ("goal", "thread-current", "paused"),
                    ("goal", "thread-current", "paused"),
                ],
            )

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

    def test_app_server_starts_project_bound_thread(self):
        stdin = io.StringIO()
        stream = io.StringIO(
            json.dumps(
                {
                    "id": 1,
                    "result": {"thread": {"id": "thread-1"}},
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

        thread = client.start_thread(service_name="auto-research-test")

        request = json.loads(stdin.getvalue())
        self.assertEqual(thread["id"], "thread-1")
        self.assertEqual(request["method"], "thread/start")
        self.assertEqual(
            request["params"],
            {"cwd": "/tmp/project", "serviceName": "auto-research-test"},
        )

    def test_managed_app_server_accepts_long_thread_responses(self):
        lifecycle = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"socketPath": "/tmp/app-server.sock"}),
        )
        websocket = SimpleNamespace(close=lambda: None)
        config = SimpleNamespace(app_server_response_timeout_s=60.0)
        with (
            patch("auto_research.app_server.subprocess.run", return_value=lifecycle),
            patch(
                "auto_research.app_server.unix_connect", return_value=websocket
            ) as connect,
        ):
            client = AppServerClient("/tmp", config=config, managed_daemon=True)
            client.close()

        self.assertEqual(
            connect.call_args.kwargs["max_size"],
            MANAGED_APP_SERVER_MAX_MESSAGE_BYTES,
        )
        self.assertIsNone(connect.call_args.kwargs["ping_interval"])

    def test_managed_app_server_can_connect_without_daemon_mutation(self):
        lifecycle = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"socketPath": "/tmp/app-server.sock"}),
        )
        websocket = SimpleNamespace(close=lambda: None)
        config = SimpleNamespace(app_server_response_timeout_s=60.0)
        with (
            patch(
                "auto_research.app_server.subprocess.run", return_value=lifecycle
            ) as run,
            patch(
                "auto_research.app_server.unix_connect", return_value=websocket
            ),
        ):
            client = AppServerClient(
                "/tmp",
                config=config,
                managed_daemon=True,
                ensure_daemon=False,
            )
            client.close()

        self.assertEqual(
            run.call_args.args[0],
            ["codex", "app-server", "daemon", "version"],
        )

    def test_runner_can_defer_worker_launch_to_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = SimpleNamespace(
                use_shell=True,
                allowed_executables=[],
                worker_heartbeat_s=5.0,
            )
            runner = ExperimentRunner(project / "research" / "runs", config=config)
            with patch("auto_research.runner.subprocess.Popen") as popen:
                run_id = runner.submit(
                    "deferred",
                    project,
                    "true",
                    60,
                    launch_worker=False,
                )
            popen.assert_not_called()
            self.assertEqual(runner.get_run(run_id)["status"], "SUBMITTED")

            process = SimpleNamespace(pid=12345, wait=lambda: 0)
            with patch(
                "auto_research.runner.subprocess.Popen", return_value=process
            ) as popen:
                launched = runner.launch(run_id)
            popen.assert_called_once()
            self.assertEqual(launched["status"], "RUNNING")

if __name__ == "__main__":
    unittest.main()

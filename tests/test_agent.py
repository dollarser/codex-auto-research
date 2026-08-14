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
from auto_research.process_identity import process_matches
from auto_research.research_session import (
    ResearchSessionError,
    ResearchSessionManager,
)
from auto_research.run_registry import add_active_run, list_active_runs
from auto_research.runner import ExperimentRunner, finalize_run
from auto_research.state_paths import thread_state_root


def write_goal(project: Path) -> None:
    project.joinpath("GOAL.md").write_text(
        "# Goal\n\nMaximize score under the fixed evaluation protocol.\n",
        encoding="utf-8",
    )


def write_managed_session(project: Path, thread_id: str) -> None:
    write_json_atomic(
        thread_state_root(project, thread_id) / "supervisor_session.json",
        {
            "schema_version": 2,
            "project_root": str(project.resolve()),
            "thread_id": thread_id,
            "title": "Test research",
            "objective": "Test objective",
            "ownership": "adopted",
            "setup_state": "ready",
            "current_cycle_id": "cycle-test",
        },
    )


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

    def require_model(self, model):
        self.calls.append(("require_model", model))
        return {"id": model, "model": model, "isDefault": True}

    def start_thread(
        self, *, service_name, model=None, approval_policy=None, sandbox=None
    ):
        self.start_count += 1
        thread_id = f"thread-{self.start_count}"
        thread = {"id": thread_id, "cwd": str(self.project)}
        self.threads[thread_id] = thread
        self.calls.append(
            ("start", service_name, str(self.project), model, approval_policy, sandbox)
        )
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

            first = manager.prepare(
                create_thread=True,
                creation_key="agent-idempotent",
                title="Dedicated Research",
            )
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
                (thread_state_root(project, first["thread_id"]) / "supervisor_session.json").read_text(
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
            client = FakeSessionAppServer(project)
            client.threads["thread-corrupt"] = {
                "id": "thread-corrupt",
                "cwd": str(project),
            }
            state_path = thread_state_root(project, "thread-corrupt") / "supervisor_session.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text("{not-json", encoding="utf-8")
            with self.assertRaisesRegex(
                ResearchSessionError, "refusing to create a duplicate"
            ):
                ResearchSessionManager(
                    project, client_factory=lambda: client
                ).prepare(thread_id="thread-corrupt")
            self.assertEqual(client.start_count, 0)

    def test_research_session_validates_objective_before_thread_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            project.joinpath("GOAL.md").write_text("   \n", encoding="utf-8")
            client = FakeSessionAppServer(project)

            with self.assertRaisesRegex(ResearchSessionError, "objective"):
                ResearchSessionManager(
                    project, client_factory=lambda: client
                ).prepare(create_thread=True, creation_key="invalid-objective")
            self.assertEqual(client.start_count, 0)
            self.assertFalse(
                thread_state_root(project, "thread-1").joinpath("supervisor_session.json").exists()
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

    def test_cli_submit_persists_command_without_starting_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_managed_session(project, "thread-supervisor")
            (project / "research").mkdir(exist_ok=True)
            (project / "research" / "config.toml").write_text(
                "[supervisor]\nevent_poll_s=0.25\n",
                encoding="utf-8",
            )
            write_json_atomic(
                thread_state_root(project, "thread-supervisor") / "supervisor" / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-supervisor",
                    "state": "OPEN",
                },
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
                with (
                    patch.dict(os.environ, {"CODEX_THREAD_ID": ""}),
                    patch(
                        "auto_research.mcp_server.ensure_supervisor_running",
                        return_value={"status": "OPERATIONAL", "operational": True},
                    ),
                ):
                    exit_code = cli_main(
                        [
                            "submit",
                            "--project",
                            str(project),
                            "--idea-id",
                            "cli-smoke",
                            "--worktree",
                            str(project),
                            "--command",
                            f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
                            "--thread-id",
                            "thread-supervisor",
                        ]
                    )
            finally:
                sys.stdout = previous
            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "SUBMITTED")
            run = ExperimentRunner(thread_state_root(project, "thread-supervisor") / "runs").get_run(
                payload["run_id"]
            )
            self.assertEqual(run["status"], "SUBMITTED")
            self.assertIsNone(run.get("worker_pid"))

    def test_config_file_and_environment_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "research").mkdir()
            (root / "research" / "config.toml").write_text(
                "[supervisor]\nevent_poll_s=0.5\ngoal_turn_timeout_s=123\n",
                encoding="utf-8",
            )
            previous = os.environ.get("AUTO_RESEARCH_EVENT_POLL_S")
            os.environ["AUTO_RESEARCH_EVENT_POLL_S"] = "0.75"
            try:
                config = load_config(root)
            finally:
                if previous is None:
                    os.environ.pop("AUTO_RESEARCH_EVENT_POLL_S", None)
                else:
                    os.environ["AUTO_RESEARCH_EVENT_POLL_S"] = previous
            self.assertEqual(config.event_poll_s, 0.75)
            self.assertEqual(config.goal_turn_timeout_s, 123)
            self.assertEqual(config.codex_model, "gpt-5.6-terra")
            self.assertEqual(config.codex_approval_policy, "never")
            self.assertEqual(config.codex_sandbox, "workspace-write")

    def test_cli_init_writes_supervisor_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with patch("sys.stdout", output):
                exit_code = cli_main(["init", tmp])

            self.assertEqual(exit_code, 0)
            goal = (Path(tmp) / "GOAL.md").read_text(encoding="utf-8")
            self.assertIn("# Goal", goal)
            config = (Path(tmp) / "research" / "config.toml").read_text(
                encoding="utf-8"
            )
            self.assertIn("[supervisor]", config)
            self.assertNotIn("[listener]", config)

    def test_cli_goal_pause_is_the_parameterized_experiment_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_managed_session(project, "thread-goal")
            state_root = thread_state_root(project, "thread-goal")
            write_json_atomic(
                state_root / "supervisor" / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-goal",
                    "state": "OPEN",
                },
            )
            add_active_run(
                state_root, run_id="run-one", thread_id="thread-goal"
            )
            add_active_run(
                state_root, run_id="run-two", thread_id="thread-goal"
            )
            client = FakeAppServer(goal_status="active")
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-goal"}),
                patch("auto_research.app_server.AppServerClient", return_value=client),
                patch("sys.stdout", output),
            ):
                exit_code = cli_main(
                    [
                        "goal",
                        "set-status",
                        "paused",
                        "--project",
                        str(project),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn(("goal", "thread-goal", "paused"), client.calls)
            markers = list_active_runs(state_root)
            self.assertEqual(len(markers), 2)
            self.assertTrue(
                all(set(marker) == {"run_id", "thread_id"} for marker in markers)
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["bridge"], "direct")
            self.assertNotIn("experiment_handoff", payload)

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
        self.assertIn("required = true", rendered)

    def test_submit_experiment_requires_managed_supervisor_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            service = ExperimentService(project, thread_id="thread-any", ensure_supervisor=False)

            with patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):  # noqa: SIM117
                with self.assertRaisesRegex(ValueError, "managed Supervisor"):
                    service.submit_experiment(
                        "idea-one", ".", "python train.py", 30, thread_id="thread-any"
                    )

    def test_submit_experiment_defers_worker_launch_to_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_managed_session(project, "thread-supervisor")
            write_json_atomic(
                thread_state_root(project, "thread-supervisor") / "supervisor" / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-supervisor",
                    "state": "OPEN",
                },
            )
            service = ExperimentService(project, thread_id="thread-supervisor", ensure_supervisor=False)

            with (
                patch.dict(os.environ, {"CODEX_THREAD_ID": ""}),
                patch.object(service.runner, "submit", return_value="run-test") as submit,
            ):
                result = service.submit_experiment(
                    "idea-one",
                    ".",
                    "python train.py",
                    30,
                    thread_id="thread-supervisor",
                )

            self.assertEqual(result["status"], "SUBMITTED")
            self.assertEqual(result["scheduler"], "app_server_supervisor")
            self.assertEqual(result["worker_owner"], "supervisor")
            self.assertFalse(submit.call_args.kwargs["launch_worker"])
            with (
                patch.object(service.runner, "get_result", return_value=None),
                patch.object(
                    service.runner,
                    "get_run",
                    return_value={"run_id": "run-test", "status": "SUBMITTED"},
                ),
            ):
                self.assertEqual(
                    service.get_experiment_result("run-test")["status"],
                    "SUBMITTED",
                )
            marker = list_active_runs(thread_state_root(project, "thread-supervisor"))[0]
            self.assertEqual(
                marker,
                {"run_id": "run-test", "thread_id": "thread-supervisor"},
            )

    def test_submit_accepts_external_worktree_long_timeout_and_shell_command(self):
        with tempfile.TemporaryDirectory() as project_tmp, tempfile.TemporaryDirectory() as work_tmp:
            project = Path(project_tmp)
            worktree = Path(work_tmp)
            write_goal(project)
            write_managed_session(project, "thread-supervisor")
            write_json_atomic(
                thread_state_root(project, "thread-supervisor")
                / "supervisor"
                / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-supervisor",
                    "state": "OPEN",
                },
            )
            service = ExperimentService(
                project, thread_id="thread-supervisor", ensure_supervisor=False
            )

            with patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):
                result = service.submit_experiment(
                    "external-long-run",
                    str(worktree),
                    "bash -lc 'echo ready && true'",
                    8 * 24 * 3600,
                    thread_id="thread-supervisor",
                )

            run = service.runner.get_run(result["run_id"])
            self.assertEqual(run["worktree"], str(worktree.resolve()))
            self.assertEqual(run["timeout_s"], 8 * 24 * 3600)
            self.assertTrue(run["shell"])

    def test_submit_ensures_a_ready_supervisor_after_registry_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_managed_session(project, "thread-supervisor")
            write_json_atomic(
                thread_state_root(project, "thread-supervisor") / "supervisor" / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-supervisor",
                    "state": "OPEN",
                },
            )
            service = ExperimentService(project, thread_id="thread-supervisor")
            with (
                patch.dict(os.environ, {"CODEX_THREAD_ID": ""}),
                patch.object(service.runner, "submit", return_value="run-test"),
                patch(
                    "auto_research.mcp_server.ensure_supervisor_running",
                    return_value={"status": "OPERATIONAL", "pid": 123, "operational": True},
                ) as ensure,
            ):
                result = service.submit_experiment(
                    "idea-one",
                    ".",
                    "python train.py",
                    30,
                    thread_id="thread-supervisor",
                )

            ensure.assert_called_once()
            self.assertEqual(result["supervisor"]["status"], "OPERATIONAL")
            self.assertEqual(
                list_active_runs(thread_state_root(project, "thread-supervisor"))[0]["run_id"], "run-test"
            )

    def test_submit_returns_durable_run_when_supervisor_needs_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            write_managed_session(project, "thread-supervisor")
            service = ExperimentService(project, thread_id="thread-supervisor")

            with (
                patch.dict(os.environ, {"CODEX_THREAD_ID": ""}),
                patch(
                    "auto_research.mcp_server.ensure_supervisor_running",
                    side_effect=RuntimeError("daemon unavailable"),
                ),
            ):
                result = service.submit_experiment(
                    "repairable",
                    ".",
                    "python train.py",
                    30,
                    thread_id="thread-supervisor",
                )

            self.assertEqual(result["status"], "SUBMITTED")
            self.assertEqual(result["supervisor"]["status"], "REPAIR_PENDING")
            run = service.runner.get_run(result["run_id"])
            self.assertEqual(run["goal_cycle_id"], "cycle-test")

    def test_submit_rejects_thread_that_is_not_managed_by_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_goal(project)
            service = ExperimentService(project, thread_id="thread-other", ensure_supervisor=False)
            previous = os.environ.get("CODEX_THREAD_ID")
            os.environ["CODEX_THREAD_ID"] = "thread-other"
            try:
                with self.assertRaisesRegex(ValueError, "managed Supervisor"):
                    service.submit_experiment(
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
            write_managed_session(project, "t1")
            write_json_atomic(
                thread_state_root(project, "t1") / "supervisor" / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": "t1",
                    "state": "OPEN",
                },
            )
            service = ExperimentService(project, thread_id="t1", ensure_supervisor=False)
            code = (
                "import json,os,pathlib; "
                "pathlib.Path(os.environ['AUTO_RESEARCH_RUN_DIR'],'metrics.json')"
                ".write_text(json.dumps({'score': 1.0}))"
            )
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
            with patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):
                first = service.submit_experiment(
                    "idea-one",
                    ".",
                    command,
                    30,
                    idempotency_key="same",
                    thread_id="t1",
                )
                service.runner.launch(first["run_id"])
                service.runner.wait(first["run_id"], poll_s=0.02)
                service.get_experiment_result(first["run_id"])
                second = service.submit_experiment(
                    "idea-one",
                    ".",
                    command,
                    30,
                    idempotency_key="same",
                    thread_id="t1",
                )
            self.assertEqual(first["run_id"], second["run_id"])

    def test_idempotency_key_rejects_a_different_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(Path(tmp) / "runs")
            runner.submit(
                "idea-one",
                tmp,
                "python first.py",
                30,
                idempotency_key="request-one",
                launch_worker=False,
            )

            with self.assertRaisesRegex(ValueError, "different request"):
                runner.submit(
                    "idea-one",
                    tmp,
                    "python second.py",
                    30,
                    idempotency_key="request-one",
                    launch_worker=False,
                )

    def test_finalize_run_preserves_latest_process_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-test"
            (run_dir / "events").mkdir(parents=True)
            stale = {"run_id": "run-test", "idea_id": "idea", "status": "RUNNING"}
            write_json_atomic(
                run_dir / "run.json",
                {**stale, "worker_pid": 10, "child_pid": 20},
            )

            finalize_run(
                run_dir,
                "completed.json",
                {"status": "COMPLETED", "run_id": "run-test"},
                stale,
            )

            persisted = json.loads((run_dir / "run.json").read_text())
            self.assertEqual(persisted["worker_pid"], 10)
            self.assertEqual(persisted["child_pid"], 20)

    def test_cancel_cleanup_failure_does_not_commit_terminal_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(Path(tmp) / "runs")
            run_id = runner.submit(
                "cancel-failure", tmp, "python train.py", 30, launch_worker=False
            )
            run_dir = runner.runs_dir / run_id
            run = runner.get_run(run_id)
            write_json_atomic(
                run_dir / "run.json",
                {
                    **run,
                    "status": "RUNNING",
                    "worker_pid": 42,
                    "worker_pid_start_ticks": 123,
                },
            )

            with (
                patch(
                    "auto_research.runner.terminate_process_group",
                    return_value=False,
                ),
                self.assertRaisesRegex(RuntimeError, "did not stop all processes"),
            ):
                runner.cancel(run_id)

            self.assertIsNone(runner.get_result(run_id))
            self.assertEqual(runner.get_run(run_id)["status"], "RUNNING")
            self.assertTrue((run_dir / "cancel.error.json").is_file())

    def test_dead_worker_without_terminal_event_becomes_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(Path(tmp) / "runs")
            run_id = runner.submit(
                "dead-worker", tmp, "python train.py", 30, launch_worker=False
            )
            run_dir = runner.runs_dir / run_id
            run = runner.get_run(run_id)
            write_json_atomic(
                run_dir / "run.json",
                {
                    **run,
                    "status": "RUNNING",
                    "worker_pid": 2_000_000_000,
                    "worker_pid_start_ticks": 1,
                },
            )

            result = runner.reconcile_worker(run_id)

            self.assertIsNotNone(result)
            self.assertEqual(result.status, "LOST")
            self.assertTrue((run_dir / "events" / "lost.json").is_file())

    def test_alive_worker_past_deadline_is_cleaned_and_times_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(Path(tmp) / "runs")
            run_id = runner.submit(
                "hung-worker", tmp, "python train.py", 1, launch_worker=False
            )
            run_dir = runner.runs_dir / run_id
            run = runner.get_run(run_id)
            write_json_atomic(
                run_dir / "run.json",
                {
                    **run,
                    "status": "RUNNING",
                    "worker_pid": 101,
                    "worker_pid_start_ticks": 1001,
                    "child_pid": 202,
                    "child_pid_start_ticks": 2002,
                },
            )
            write_json_atomic(
                run_dir / "events" / "started.json",
                {"run_id": run_id, "started_at": 1.0},
            )
            write_json_atomic(
                run_dir / "heartbeat.json",
                {"run_id": run_id, "worker_pid": 101, "timestamp": 1.0},
            )

            with (
                patch("auto_research.runner.process_identity_state", return_value="alive"),
                patch(
                    "auto_research.runner.terminate_process_group", return_value=True
                ) as terminate,
            ):
                result = runner.reconcile_worker(run_id, now=100.0)

            self.assertIsNotNone(result)
            self.assertEqual(result.status, "TIMEOUT")
            self.assertIn("heartbeat=stale", result.error)
            self.assertEqual(terminate.call_count, 2)

    def test_unverifiable_worker_identity_does_not_commit_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(Path(tmp) / "runs")
            run_id = runner.submit(
                "unknown-worker", tmp, "python train.py", 30, launch_worker=False
            )
            run_dir = runner.runs_dir / run_id
            run = runner.get_run(run_id)
            write_json_atomic(run_dir / "run.json", {**run, "status": "RUNNING"})

            with self.assertRaisesRegex(RuntimeError, "could not be verified"):
                runner.reconcile_worker(run_id)

            self.assertIsNone(runner.get_result(run_id))
            self.assertEqual(runner.get_run(run_id)["status"], "RUNNING")
            self.assertTrue((run_dir / "worker_identity.error.json").is_file())

    def test_corrupt_unfinished_run_metadata_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(Path(tmp) / "runs")
            run_dir = runner.runs_dir / "run-corrupt"
            run_dir.mkdir()
            (run_dir / "run.json").write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Invalid run metadata"):
                runner.list_unfinished(thread_id="thread-test")

    def test_process_completion_and_artifact_validation_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(Path(tmp) / "runs")
            run_id = runner.submit("no-metrics", tmp, "true", 30)

            result = runner.wait(run_id, poll_s=0.02)

            self.assertEqual(result.status, "COMPLETED")
            self.assertFalse(result.artifact_validation["valid"])
            self.assertIn("metrics.json", result.artifact_validation["errors"][0])

    def test_cancel_confirms_verified_worker_and_child_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(Path(tmp) / "runs")
            run_id = runner.submit("cancel", tmp, "sleep 30", 60)
            deadline = time.monotonic() + 5
            run = runner.get_run(run_id)
            while not run.get("child_pid") and time.monotonic() < deadline:
                time.sleep(0.02)
                run = runner.get_run(run_id)

            result = runner.cancel(run_id)

            self.assertEqual(result.status, "CANCELLED")
            latest = runner.get_run(run_id)
            self.assertFalse(
                process_matches(
                    latest.get("child_pid"), latest.get("child_pid_start_ticks")
                )
            )
            self.assertFalse(
                process_matches(
                    latest.get("worker_pid"), latest.get("worker_pid_start_ticks")
                )
            )

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

        thread = client.start_thread(
            service_name="auto-research-test",
            model="gpt-5.6-terra",
            approval_policy="never",
            sandbox="workspace-write",
        )

        request = json.loads(stdin.getvalue())
        self.assertEqual(thread["id"], "thread-1")
        self.assertEqual(request["method"], "thread/start")
        self.assertEqual(
            request["params"],
            {
                "cwd": "/tmp/project",
                "serviceName": "auto-research-test",
                "model": "gpt-5.6-terra",
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
            },
        )

    def test_app_server_model_preflight_uses_account_catalog(self):
        stdin = io.StringIO()
        stream = io.StringIO(
            json.dumps(
                {
                    "id": 1,
                    "result": {
                        "data": [
                            {
                                "id": "gpt-5.6-terra",
                                "model": "gpt-5.6-terra",
                                "isDefault": True,
                            }
                        ]
                    },
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

        model = client.require_model("gpt-5.6-terra")

        request = json.loads(stdin.getvalue())
        self.assertEqual(model["model"], "gpt-5.6-terra")
        self.assertEqual(request["method"], "model/list")
        self.assertTrue(request["params"]["includeHidden"])

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

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from auto_research.app_server import AppServerTimeout
from auto_research.cli import main as cli_main
from auto_research.ledger import write_json_atomic
from auto_research.process_identity import (
    process_matches,
    process_start_ticks,
    terminate_process_group,
)
from auto_research.research_session import ResearchSessionError, ResearchSessionManager
from auto_research.state_paths import thread_state_root
from auto_research.supervisor import (
    SupervisorError,
    SupervisorOwnershipError,
    read_supervisor_state,
    supervisor_dir,
)
from auto_research.supervisor_process import (
    _scheduler_lock_is_held,
    _wait_repair_turn,
    read_supervisor_process,
    restart_supervisor,
    spawn_supervisor,
)
from auto_research.supervisor_process import (
    main as supervisor_process_main,
)


class SessionClient:
    def __init__(self, project: Path):
        self.project = project.resolve()
        self.threads: dict[str, dict] = {}
        self.goals: dict[str, dict] = {}
        self.start_count = 0
        self._lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def initialize(self):
        return None

    def start_thread(self, **_kwargs):
        with self._lock:
            self.start_count += 1
            thread_id = f"thread-{self.start_count}"
            value = {"id": thread_id, "cwd": str(self.project)}
            self.threads[thread_id] = value
            return value

    def read_thread(self, thread_id):
        return self.threads[thread_id]

    def set_thread_name(self, _thread_id, _name):
        return None

    def get_goal(self, thread_id):
        return self.goals.get(thread_id)

    def set_goal(self, thread_id, *, objective=None, status=None):
        goal = dict(self.goals.get(thread_id, {}))
        goal.update({"threadId": thread_id, "objective": objective, "status": status})
        self.goals[thread_id] = goal
        return goal


class ThreadStateTests(unittest.TestCase):
    def test_process_repair_wait_reconciles_runs_between_turn_polls(self):
        class RepairClient:
            def __init__(self):
                self.read_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def initialize(self):
                return None

            def read_thread(self, thread_id, *, include_turns=False):
                self.read_count += 1
                status = "inProgress" if self.read_count == 1 else "completed"
                return {
                    "id": thread_id,
                    "turns": [{"id": "repair-turn", "status": status}],
                }

            def wait_turn(self, thread_id, turn_id, *, timeout_s=None):
                raise AppServerTimeout("poll")

        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            client = RepairClient()
            monitor = unittest.mock.Mock()
            with (
                patch(
                    "auto_research.supervisor_process.AppServerClient",
                    return_value=client,
                ),
                patch(
                    "auto_research.supervisor_process.GoalRuntimeSupervisor",
                    return_value=monitor,
                ),
            ):
                completed = _wait_repair_turn(
                    project, "thread-goal", "repair-turn"
                )

            self.assertEqual(completed["status"], "completed")
            monitor._launch_or_observe_experiments.assert_called_once_with(
                client, "thread-goal"
            )

    def test_duplicate_process_rejection_does_not_report_shared_failure(self):
        rejected = unittest.mock.Mock()
        rejected.run.side_effect = SupervisorOwnershipError(
            "another Supervisor owns this Thread"
        )
        output = io.StringIO()
        with (
            patch(
                "auto_research.supervisor_process.GoalRuntimeSupervisor",
                return_value=rejected,
            ),
            patch("auto_research.supervisor_process._report_bootstrap_failure") as report,
            patch("sys.stdout", output),
        ):
            exit_code = supervisor_process_main(
                ["--project", "/tmp/project", "--thread-id", "thread-goal"]
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["state"], "ALREADY_OWNED")
        report.assert_not_called()

    def test_failed_repair_keeps_monitoring_an_active_experiment(self):
        supervisor = unittest.mock.Mock()
        supervisor.run.side_effect = [
            RuntimeError("controller failed"),
            {"state": "OPEN"},
        ]
        supervisor.report_fatal_error.return_value = {
            "state": "NEEDS_USER",
            "recovery_turn_id": "repair-turn",
        }
        supervisor._write_state.return_value = {"state": "NEEDS_USER"}
        supervisor._active_experiments.return_value = [{"run_id": "run-live"}]
        output = io.StringIO()
        with (
            patch(
                "auto_research.supervisor_process.GoalRuntimeSupervisor",
                return_value=supervisor,
            ),
            patch(
                "auto_research.supervisor_process._wait_repair_turn",
                side_effect=TimeoutError("repair stalled"),
            ),
            patch("sys.stdout", output),
        ):
            exit_code = supervisor_process_main(
                ["--project", "/tmp/project", "--thread-id", "thread-goal"]
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["state"], "OPEN")
        self.assertEqual(supervisor.run.call_count, 2)
        supervisor._active_experiments.assert_called_once_with("thread-goal")

    def test_untracked_scheduler_lock_prevents_duplicate_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "supervisor"
            control.mkdir()
            lock = control / "scheduler.lock"
            lock.touch()
            import fcntl

            with lock.open("r+") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(_scheduler_lock_is_held(control))

    def test_process_identity_rejects_pid_with_wrong_start_time(self):
        pid = os.getpid()
        start = process_start_ticks(pid)
        self.assertIsNotNone(start)
        self.assertTrue(process_matches(pid, start))
        self.assertFalse(process_matches(pid, int(start) + 1))

    def test_terminate_live_process_without_identity_fails_closed(self):
        with (
            patch("auto_research.process_identity.os.kill") as kill,
            patch("auto_research.process_identity.os.killpg") as killpg,
        ):
            self.assertFalse(terminate_process_group(42, None))
        kill.assert_called_once_with(42, 0)
        killpg.assert_not_called()

    def test_inaccessible_proc_does_not_delete_supervisor_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            control = supervisor_dir(project, "thread-goal")
            control.mkdir(parents=True)
            process_path = control / "process.json"
            write_json_atomic(
                process_path,
                {"pid": 42, "pid_start_ticks": 123, "status": "OPERATIONAL"},
            )
            with (
                patch("auto_research.supervisor_process.os.kill"),
                patch(
                    "auto_research.supervisor_process.process_start_ticks",
                    return_value=None,
                ),
            ):
                self.assertIsNone(read_supervisor_process(project, "thread-goal"))

            self.assertTrue(process_path.exists())

    def test_unstoppable_starting_supervisor_retains_process_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            client = SessionClient(project)
            session = ResearchSessionManager(
                project, client_factory=lambda: client
            ).prepare(create_thread=True, creation_key="starting-owner")
            process_path = (
                supervisor_dir(project, session["thread_id"]) / "process.json"
            )
            write_json_atomic(
                process_path,
                {
                    "pid": os.getpid(),
                    "pid_start_ticks": process_start_ticks(os.getpid()),
                    "status": "STARTING",
                },
            )

            with (
                patch(
                    "auto_research.supervisor_process.terminate_process_group",
                    return_value=False,
                ),
                self.assertRaisesRegex(SupervisorError, "retaining process identity"),
            ):
                spawn_supervisor(project, thread_id=session["thread_id"])

            self.assertTrue(process_path.exists())

    def _project(self, tmp: str) -> Path:
        project = Path(tmp)
        project.joinpath("GOAL.md").write_text("# Goal\n\nImprove tail recall.\n")
        return project

    def test_new_thread_immediately_owns_metadata_session_and_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            client = SessionClient(project)
            result = ResearchSessionManager(
                project, client_factory=lambda: client
            ).prepare(
                create_thread=True,
                creation_key="metadata-test",
                title="Tail research",
            )

            root = thread_state_root(project, result["thread_id"])
            self.assertEqual(Path(result["state_root"]), root)
            self.assertTrue((root / "metadata.json").is_file())
            self.assertTrue((root / "supervisor_session.json").is_file())
            self.assertEqual(len(list((root / "cycles").glob("*.json"))), 1)

    def test_same_thread_new_goal_cycle_reuses_root_and_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            client = SessionClient(project)
            manager = ResearchSessionManager(project, client_factory=lambda: client)
            first = manager.prepare(create_thread=True, creation_key="cycle-test")
            root = thread_state_root(project, first["thread_id"])
            sentinel = root / "runs" / "run-old" / "result.json"
            write_json_atomic(sentinel, {"status": "COMPLETED"})
            client.goals[first["thread_id"]]["status"] = "complete"

            second = manager.restart_goal(
                objective="Improve the next tail failure."
            )

            self.assertEqual(first["thread_id"], second["thread_id"])
            self.assertNotEqual(first["cycle_id"], second["cycle_id"])
            self.assertTrue(sentinel.is_file())
            self.assertEqual(len(list((root / "cycles").glob("*.json"))), 2)

    def test_concurrent_start_on_one_manager_creates_one_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            client = SessionClient(project)
            manager = ResearchSessionManager(project, client_factory=lambda: client)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(
                        lambda _: manager.prepare(
                            create_thread=True, creation_key="concurrent-test"
                        ),
                        range(2),
                    )
                )

            self.assertEqual(client.start_count, 1)
            self.assertEqual({item["thread_id"] for item in results}, {"thread-1"})

    def test_independent_managers_share_creation_key_without_duplicate_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            client = SessionClient(project)

            def create() -> dict:
                return ResearchSessionManager(
                    project, client_factory=lambda: client
                ).prepare(create_thread=True, creation_key="request-42")

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: create(), range(2)))

            self.assertEqual(client.start_count, 1)
            self.assertEqual({item["thread_id"] for item in results}, {"thread-1"})

    def test_completed_bound_goal_requires_explicit_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            client = SessionClient(project)
            manager = ResearchSessionManager(project, client_factory=lambda: client)
            first = manager.prepare(create_thread=True, creation_key="explicit-restart")
            client.goals[first["thread_id"]]["status"] = "complete"

            with self.assertRaisesRegex(ResearchSessionError, "supervisor restart"):
                manager.prepare(create_thread=True, objective="Do not restart implicitly")

    def test_damaged_supervisor_binding_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            write_json_atomic(
                thread_state_root(project, "thread-a") / "supervisor" / "state.json",
                {"thread_id": "thread-b", "state": "OPEN"},
            )
            with self.assertRaisesRegex(SupervisorError, "disagrees"):
                read_supervisor_state(project, "thread-a")

    def test_state_root_option_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(io.StringIO()):  # noqa: SIM117
            with self.assertRaises(SystemExit):
                cli_main(
                    [
                        "goal",
                        "set-status",
                        "paused",
                        "--project",
                        tmp,
                        "--state-root",
                        "research/arbitrary",
                    ]
                )

    def test_supervisor_start_without_thread_identity_fails_before_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            with patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):  # noqa: SIM117
                with self.assertRaisesRegex(SupervisorError, "requires --thread-id"):
                    spawn_supervisor(project)

    def test_external_status_uses_explicit_thread_not_calling_task_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            write_json_atomic(
                supervisor_dir(project, "thread-target") / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": "thread-target",
                    "state": "OPEN",
                },
            )
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-caller"}),
                patch("sys.stdout", output),
            ):
                exit_code = cli_main(
                    [
                        "supervisor",
                        "status",
                        "--project",
                        str(project),
                        "--thread-id",
                        "thread-target",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["thread_id"], "thread-target")

    def test_repeated_start_with_same_thread_reuses_live_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            client = SessionClient(project)
            session = ResearchSessionManager(
                project, client_factory=lambda: client
            ).prepare(create_thread=True, creation_key="repeat-start")
            write_json_atomic(
                supervisor_dir(project, session["thread_id"]) / "process.json",
                {
                    "pid": os.getpid(),
                    "pid_start_ticks": process_start_ticks(os.getpid()),
                    "status": "OPERATIONAL",
                },
            )

            result = spawn_supervisor(project, thread_id=session["thread_id"])

            self.assertEqual(result["status"], "ALREADY_RUNNING")
            self.assertEqual(result["thread_id"], session["thread_id"])
            self.assertEqual(client.start_count, 1)

    def test_restart_reuses_thread_root_and_creates_new_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            client = SessionClient(project)
            first = ResearchSessionManager(
                project, client_factory=lambda: client
            ).prepare(create_thread=True, creation_key="restart-test")
            thread_id = first["thread_id"]
            root = thread_state_root(project, thread_id)
            sentinel = root / "runs" / "old-run" / "result.json"
            write_json_atomic(sentinel, {"status": "COMPLETED"})
            client.goals[thread_id]["status"] = "complete"
            write_json_atomic(
                supervisor_dir(project, thread_id) / "state.json",
                {
                    "schema_version": 3,
                    "project_root": str(project.resolve()),
                    "thread_id": thread_id,
                    "state": "COMPLETED",
                },
            )

            with (
                patch(
                    "auto_research.research_session.AppServerClient",
                    return_value=client,
                ),
                patch(
                    "auto_research.supervisor_process.spawn_supervisor",
                    return_value={"status": "OPERATIONAL"},
                ),
            ):
                result = restart_supervisor(
                    project,
                    thread_id=thread_id,
                    objective="Second research cycle",
                )

            self.assertEqual(result["thread_id"], thread_id)
            self.assertTrue(sentinel.is_file())
            self.assertEqual(len(list((root / "cycles").glob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()

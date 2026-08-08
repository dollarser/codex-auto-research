from __future__ import annotations

import json
import io
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from auto_research.goal_harness import _event_turn_id, _extract_run_ids, AppServerTimeoutError
from auto_research.goal_harness import (
    AppServerClient,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_REASONING_EFFORT,
)
from auto_research.config import load_harness_config
from auto_research.mcp_server import ExperimentService
from auto_research.models import GoalSpec
from auto_research.runner import ExperimentRunner, finalize_run
from auto_research.ledger import write_json_atomic
from auto_research.mcp_config import register_mcp_config


class AgentTests(unittest.TestCase):
    def test_harness_config_file_and_environment_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "research").mkdir()
            (root / "research" / "harness.toml").write_text(
                "[codex]\nmodel = 'file-model'\nreasoning_effort = 'low'\n"
                "[harness]\nmax_cycles = 7\n[experiment]\nuse_shell = false\n",
                encoding="utf-8",
            )
            old_model = os.environ.get("AUTO_RESEARCH_CODEX_MODEL")
            old_cycles = os.environ.get("AUTO_RESEARCH_MAX_CYCLES")
            os.environ["AUTO_RESEARCH_CODEX_MODEL"] = "env-model"
            os.environ["AUTO_RESEARCH_MAX_CYCLES"] = "9"
            try:
                config = load_harness_config(root)
            finally:
                if old_model is None:
                    os.environ.pop("AUTO_RESEARCH_CODEX_MODEL", None)
                else:
                    os.environ["AUTO_RESEARCH_CODEX_MODEL"] = old_model
                if old_cycles is None:
                    os.environ.pop("AUTO_RESEARCH_MAX_CYCLES", None)
                else:
                    os.environ["AUTO_RESEARCH_MAX_CYCLES"] = old_cycles
            self.assertEqual(config.codex_model, "env-model")
            self.assertEqual(config.codex_reasoning_effort, "low")
            self.assertEqual(config.max_cycles, 9)
            self.assertFalse(config.use_shell)

    def test_app_server_model_overrides_are_configurable(self):
        client = AppServerClient.__new__(AppServerClient)
        client.model = "gpt-test"
        client.reasoning_effort = "high"
        self.assertEqual(client._model_overrides(), {"model": "gpt-test", "effort": "high"})

    def test_app_server_model_overrides_omit_unset_values(self):
        client = AppServerClient.__new__(AppServerClient)
        client.model = None
        client.reasoning_effort = None
        self.assertEqual(client._model_overrides(), {})

    def test_harness_defaults_are_pinned(self):
        self.assertEqual(DEFAULT_CODEX_MODEL, "gpt-5.6-luna")
        self.assertEqual(DEFAULT_CODEX_REASONING_EFFORT, "medium")

    def test_register_mcp_preserves_other_codex_config(self):
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
            self.assertIn('[profiles.default]\nmodel = "gpt-test"', content)
            self.assertIn('[mcp_servers.other]\ncommand = "other"', content)
            self.assertIn("auto_research.mcp_server", content)
            self.assertEqual(content.count("[mcp_servers.experiment]"), 1)

    def test_render_mcp_config_preserves_virtualenv_entrypoint(self):
        from auto_research.mcp_config import render_mcp_config

        rendered = render_mcp_config(".", "/tmp/project/.venv/bin/python")
        self.assertIn('command = "/tmp/project/.venv/bin/python"', rendered)

    def test_extract_run_id_from_mcp_notification(self):
        notification = {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "mcpToolCall",
                    "result": {
                        "structuredContent": {
                            "run_id": "run-test-001",
                            "status": "RUNNING",
                        }
                    },
                }
            },
        }
        self.assertEqual(_extract_run_ids(notification), {"run-test-001"})

    def test_event_turn_id_filters_replayed_history(self):
        historical = {
            "method": "item/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-old", "item": {
                "type": "mcpToolCall", "result": {"structuredContent": {"run_id": "run-old"}}
            }},
        }
        current = {
            "method": "item/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-new", "item": {
                "type": "mcpToolCall", "result": {"structuredContent": {"run_id": "run-new"}}
            }},
        }
        self.assertEqual(_event_turn_id(historical), "turn-old")
        self.assertEqual(_event_turn_id(current), "turn-new")
        self.assertNotEqual(_event_turn_id(historical), "turn-new")

    def test_app_server_client_ignores_previous_turn_mcp_items(self):
        from auto_research.goal_harness import AppServerClient

        stream = io.StringIO("\n".join([
            json.dumps({"id": 1, "result": {"turn": {"id": "turn-new"}}}),
            json.dumps({"method": "item/completed", "params": {"turnId": "turn-old", "item": {
                "type": "mcpToolCall", "result": {"structuredContent": {"run_id": "run-old"}}
            }}}),
            json.dumps({"method": "item/completed", "params": {"turnId": "turn-new", "item": {
                "type": "mcpToolCall", "result": {"structuredContent": {"run_id": "run-new"}}
            }}}),
            json.dumps({"method": "turn/completed", "params": {"turn": {"id": "turn-old"}}}),
            json.dumps({"method": "turn/completed", "params": {"turn": {"id": "turn-new"}}}),
        ]) + "\n")
        client = AppServerClient.__new__(AppServerClient)
        client.cwd = "."
        client.approval_policy = "never"
        client.sandbox = "danger-full-access"
        client.model = None
        client.reasoning_effort = None
        client.config = SimpleNamespace(app_server_response_timeout_s=60.0, app_server_turn_timeout_s=900.0)
        client._next_id = 1
        client.process = SimpleNamespace(stdin=io.StringIO(), stdout=stream)
        started = []
        client.on_turn_started = started.append
        self.assertEqual(client.start_turn("thread-1", "continue"), {"run-new"})
        self.assertEqual(started, ["turn-new"])

    def test_app_server_stdout_timeout_is_detected(self):
        read_fd, write_fd = os.pipe()
        try:
            client = AppServerClient.__new__(AppServerClient)
            client.process = SimpleNamespace(stdout=os.fdopen(read_fd, "r"))
            client._stderr_tail = lambda: ""
            with self.assertRaises(AppServerTimeoutError):
                client._readline_with_timeout(0.01, "test turn")
        finally:
            os.close(write_fd)
            try:
                client.process.stdout.close()
            except (AttributeError, OSError):
                pass

    def test_app_server_answers_non_interactive_server_requests(self):
        stream = io.StringIO("\n".join([
            json.dumps({"id": 99, "method": "item/commandExecution/requestApproval", "params": {}}),
            json.dumps({"id": 1, "result": {"turn": {"id": "turn-new"}}}),
            json.dumps({"method": "turn/completed", "params": {"turn": {"id": "turn-new"}}}),
        ]) + "\n")
        stdin = io.StringIO()
        client = AppServerClient.__new__(AppServerClient)
        client.cwd = "."
        client.approval_policy = "never"
        client.sandbox = "danger-full-access"
        client.model = None
        client.reasoning_effort = None
        client.config = SimpleNamespace(app_server_response_timeout_s=60.0, app_server_turn_timeout_s=900.0)
        client._next_id = 1
        client.process = SimpleNamespace(stdin=stdin, stdout=stream)
        self.assertEqual(client.start_turn("thread-1", "continue"), set())
        self.assertIn('"decision": "decline"', stdin.getvalue())

    def test_app_server_buffers_turn_events_received_before_start_response(self):
        stream = io.StringIO("\n".join([
            json.dumps({"method": "turn/completed", "params": {"turn": {"id": "turn-new"}}}),
            json.dumps({"id": 1, "result": {"turn": {"id": "turn-new"}}}),
        ]) + "\n")
        client = AppServerClient.__new__(AppServerClient)
        client.cwd = "."
        client.approval_policy = "never"
        client.sandbox = "danger-full-access"
        client.model = None
        client.reasoning_effort = None
        client.config = SimpleNamespace(app_server_response_timeout_s=60.0, app_server_turn_timeout_s=900.0)
        client._next_id = 1
        client.process = SimpleNamespace(stdin=io.StringIO(), stdout=stream)
        self.assertEqual(client.start_turn("thread-1", "continue"), set())

    def test_app_server_accepts_final_answer_when_turn_completed_is_missing(self):
        stream = io.StringIO("\n".join([
            json.dumps({"id": 1, "result": {"turn": {"id": "turn-final"}}}),
            json.dumps({"method": "item/completed", "params": {"turnId": "turn-final", "item": {
                "type": "agentMessage", "phase": "final_answer", "text": "done"
            }}}),
        ]) + "\n")
        client = AppServerClient.__new__(AppServerClient)
        client.cwd = "."
        client.approval_policy = "never"
        client.sandbox = "danger-full-access"
        client.model = None
        client.reasoning_effort = None
        client.config = SimpleNamespace(app_server_response_timeout_s=60.0, app_server_turn_timeout_s=900.0)
        client._next_id = 1
        client.process = SimpleNamespace(stdin=io.StringIO(), stdout=stream)
        self.assertEqual(client.start_turn("thread-1", "continue"), set())

    def test_app_server_returns_after_durable_start_experiment_result(self):
        stream = io.StringIO("\n".join([
            json.dumps({"id": 1, "result": {"turn": {"id": "turn-started"}}}),
            json.dumps({"method": "item/completed", "params": {"turnId": "turn-started", "item": {
                "type": "mcpToolCall", "result": {"structuredContent": {
                    "run_id": "run-test-started", "status": "RUNNING"
                }}
            }}}),
        ]) + "\n")
        client = AppServerClient.__new__(AppServerClient)
        client.cwd = "."
        client.approval_policy = "never"
        client.sandbox = "danger-full-access"
        client.model = None
        client.reasoning_effort = None
        client.config = SimpleNamespace(app_server_response_timeout_s=60.0, app_server_turn_timeout_s=900.0)
        client._next_id = 1
        client.process = SimpleNamespace(stdin=io.StringIO(), stdout=stream)
        self.assertEqual(client.start_turn("thread-1", "start"), {"run-test-started"})

    def test_app_server_returns_when_durable_run_appears_without_mcp_item(self):
        stream = io.StringIO(json.dumps({"id": 1, "result": {"turn": {"id": "turn-durable"}}}) + "\n")
        client = AppServerClient.__new__(AppServerClient)
        client.cwd = "."
        client.approval_policy = "never"
        client.sandbox = "danger-full-access"
        client.model = None
        client.reasoning_effort = None
        client.config = SimpleNamespace(app_server_response_timeout_s=60.0, app_server_turn_timeout_s=900.0)
        client._next_id = 1
        client.process = SimpleNamespace(stdin=io.StringIO(), stdout=stream)
        client.run_probe = lambda: {"run-durable"}
        self.assertEqual(client.start_turn("thread-1", "start"), {"run-durable"})

    def test_runner_survives_parent_style_detached_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = ExperimentRunner(root / "runs")
            run_id = run.submit(
                "idea-test",
                root,
                "python -c 'import json,os; from pathlib import Path; Path(os.environ[\"AUTO_RESEARCH_RUN_DIR\"]).joinpath(\"metrics.json\").write_text(json.dumps({\"score\": 1.0}))'",
                10,
            )
            result = run.wait(run_id)
            self.assertEqual(result.status, "COMPLETED")
            self.assertEqual(result.metrics["score"], 1.0)

    def test_experiment_service_returns_run_id_and_recovers_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ExperimentService(root)
            response = service.start_experiment(
                "idea-mcp",
                ".",
                "python -c 'import json,os; from pathlib import Path; Path(os.environ[\"AUTO_RESEARCH_RUN_DIR\"]).joinpath(\"metrics.json\").write_text(json.dumps({\"score\": 0.8}))'",
                10,
            )
            self.assertTrue(response["run_id"].startswith("run-idea-mcp-"))
            service.runner.wait(response["run_id"])
            result = service.get_experiment_result(response["run_id"])
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(result["metrics"]["score"], 0.8)

    def test_experiment_service_can_cancel_long_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ExperimentService(root)
            response = service.start_experiment("idea-cancel", ".", "python -c 'import time; time.sleep(30)'", 60)
            result = service.cancel_experiment(response["run_id"])
            self.assertEqual(result["status"], "CANCELLED")
            self.assertEqual(service.get_experiment_result(response["run_id"])["status"], "CANCELLED")

    def test_experiment_service_rejects_second_active_experiment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ExperimentService(root)
            first = service.start_experiment("idea-first", ".", "python -c 'import time; time.sleep(30)'", 60)
            try:
                with self.assertRaisesRegex(RuntimeError, "one active experiment"):
                    service.start_experiment("idea-second", ".", "python -c 'pass'", 60)
            finally:
                service.cancel_experiment(first["run_id"])

    def test_experiment_service_rejects_second_submission_in_same_harness_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ExperimentService(root)
            write_json_atomic(service.harness_cycle_path, {
                "cycle_id": "cycle-test-1",
                "pid": os.getpid(),
            })
            first = service.start_experiment(
                "idea-cycle-first", ".",
                "python -c 'import json,os; from pathlib import Path; Path(os.environ[\"AUTO_RESEARCH_RUN_DIR\"]).joinpath(\"metrics.json\").write_text(json.dumps({\"score\": 1.0}))'",
                10,
            )
            service.runner.wait(first["run_id"])
            service.get_experiment_result(first["run_id"])
            with self.assertRaisesRegex(RuntimeError, "one experiment submission is allowed per Harness cycle"):
                service.start_experiment("idea-cycle-second", ".", "python -c 'pass'", 10)

    def test_experiment_service_recovers_from_malformed_active_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "research").mkdir()
            (root / "research" / "active_experiment.json").write_text("{broken", encoding="utf-8")
            service = ExperimentService(root)
            self.assertIsNone(service._active_run_id())
            self.assertFalse((root / "research" / "active_experiment.json").exists())

    def test_experiment_service_does_not_fallback_from_invalid_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "goal.json").write_text(json.dumps({
                "goal_id": "goal-input",
                "statement": "maximize score",
                "primary_metric": "score",
            }))
            (root / "research").mkdir()
            (root / "research" / "goal_contract.json").write_text("{broken", encoding="utf-8")
            service = ExperimentService(root)
            with self.assertRaisesRegex(ValueError, "invalid goal_contract.json"):
                service.start_experiment("idea-invalid-contract", ".", "python -c 'pass'", 10)

    def test_experiment_persists_goal_contract_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "goal.json").write_text(json.dumps({
                "goal_id": "goal-snapshot",
                "statement": "maximize ap",
                "primary_metric": "ap",
                "hard_requirements": [{"metric": "ap", "operator": ">=", "value": 0.5}],
            }))
            service = ExperimentService(root)
            response = service.start_experiment(
                "idea-snapshot",
                ".",
                "python -c 'import json,os; from pathlib import Path; Path(os.environ[\"AUTO_RESEARCH_RUN_DIR\"]).joinpath(\"metrics.json\").write_text(json.dumps({\"ap\": 0.6}))'",
                10,
            )
            run = service.runner.get_run(response["run_id"])
            self.assertEqual(run["hard_requirements_snapshot"][0]["value"], 0.5)
            service.runner.wait(response["run_id"])

    def test_runner_marks_missing_terminal_event_as_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = ExperimentRunner(root / "runs")
            run_dir = root / "runs" / "run-lost"
            (run_dir / "events").mkdir(parents=True)
            write_json_atomic(run_dir / "run.json", {
                "run_id": "run-lost",
                "idea_id": "idea-lost",
                "created_at": 0,
                "timeout_s": 1,
                "status": "RUNNING",
            })
            result = runner.wait("run-lost", grace_s=0)
            self.assertEqual(result.status, "LOST")
            self.assertTrue((run_dir / "events/lost.json").exists())

    def test_runner_idempotency_key_returns_existing_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = ExperimentRunner(root / "runs")
            command = "python -c 'import json,os; from pathlib import Path; Path(os.environ[\"AUTO_RESEARCH_RUN_DIR\"]).joinpath(\"metrics.json\").write_text(json.dumps({\"score\": 0.1}))'"
            first = runner.submit("unsafe/idea", root, command, 10, idempotency_key="goal-idea-hash")
            second = runner.submit("unsafe/idea", root, command, 10, idempotency_key="goal-idea-hash")
            self.assertEqual(first, second)
            result = runner.wait(first)
            self.assertEqual(result.status, "COMPLETED")

    def test_terminal_finalize_is_single_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            (run_dir / "events").mkdir(parents=True)
            run = {"run_id": "run", "idea_id": "idea", "status": "RUNNING"}
            event = {"event": "RUN_COMPLETED", "run_id": "run", "idea_id": "idea", "status": "COMPLETED"}
            self.assertTrue(finalize_run(run_dir, "completed.json", event, run))
            self.assertFalse(finalize_run(run_dir, "lost.json", {**event, "status": "LOST"}, run))

    def test_runner_rejects_non_allowlisted_shell_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = ExperimentRunner(root / "runs")
            old = os.environ.get("AUTO_RESEARCH_USE_SHELL")
            os.environ["AUTO_RESEARCH_USE_SHELL"] = "false"
            try:
                with self.assertRaises(ValueError):
                    runner.submit("idea-shell", root, "sh -c 'echo unsafe'", 10)
            finally:
                if old is None:
                    os.environ.pop("AUTO_RESEARCH_USE_SHELL", None)
                else:
                    os.environ["AUTO_RESEARCH_USE_SHELL"] = old

    def test_runner_rejects_path_traversal_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(Path(tmp) / "runs")
            with self.assertRaises(ValueError):
                runner.get_result("run-../outside")

    def test_runner_rejects_success_without_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = ExperimentRunner(root / "runs")
            run_id = runner.submit("no-metrics", root, "python -c 'pass'", 10)
            result = runner.wait(run_id)
            self.assertEqual(result.status, "FAILED")
            self.assertIn("metrics.json", result.error)

    def test_runner_exposes_failure_diagnostics_and_venv_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = ExperimentRunner(root / "runs")
            command = "python -c 'import os,sys; print(os.environ[\"PATH\"]); print(\"diagnostic\", file=sys.stderr); sys.exit(7)'"
            run_id = runner.submit("diagnostics", root, command, 10)
            result = runner.wait(run_id)
            self.assertEqual(result.status, "FAILED")
            self.assertEqual(result.return_code, 7)
            self.assertIn("diagnostic", result.stderr_tail)
            self.assertTrue(result.argv)
            self.assertIn(str(Path(sys.executable).parent), result.stdout_tail)

    def test_goal_harness_leaves_goal_completion_to_codex(self):
        from auto_research.goal_harness import GoalHarness

        goal = GoalSpec(
            goal_id="goal-plateau",
            statement="maximize accuracy",
            primary_metric="accuracy",
            plateau_window=3,
            metric_noise_threshold=0.001,
        )
        state = {"recent_metrics": [0.8, 0.8005, 0.8], "completed_runs": 3}
        result = type("Result", (), {"status": "COMPLETED", "metrics": {"accuracy": 0.8}})()
        self.assertIsNone(GoalHarness._should_stop(state, result, goal))

    def test_goal_harness_reconciles_terminal_pending_run(self):
        from auto_research.goal_harness import GoalHarness

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "research").mkdir()
            (root / "goal.json").write_text(json.dumps({
                "goal_id": "goal-reconcile",
                "statement": "maximize score",
                "primary_metric": "score",
                "stopping": {"max_experiments": 5, "max_consecutive_failures": 2},
            }), encoding="utf-8")
            harness = GoalHarness(root)
            run_id = harness.runner.submit(
                "idea-reconcile",
                root,
                "python -c 'import json,os; from pathlib import Path; Path(os.environ[\"AUTO_RESEARCH_RUN_DIR\"]).joinpath(\"metrics.json\").write_text(json.dumps({\"score\": 0.7}))'",
                10,
            )
            harness.runner.wait(run_id)
            state = {}
            result, failures, stop_reason = harness._consume_pending_result(state, run_id)
            self.assertEqual(result.status, "COMPLETED")
            self.assertEqual(failures, [])
            self.assertIsNone(stop_reason)
            self.assertEqual(state["completed_runs"], 1)
            self.assertIsNone(state["pending_run_id"])

    def test_goal_harness_allows_one_repair_turn_at_failure_limit(self):
        from auto_research.goal_harness import GoalHarness

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "research").mkdir()
            (root / "goal.json").write_text(json.dumps({
                "goal_id": "goal-repair",
                "statement": "maximize score",
                "primary_metric": "score",
                "stopping": {"max_experiments": 10, "max_consecutive_failures": 2},
            }), encoding="utf-8")
            harness = GoalHarness(root)
            state = {}
            for index in range(2):
                run_id = harness.runner.submit(
                    f"failed-{index}", root,
                    "python -c 'import sys; print(\"broken\", file=sys.stderr); sys.exit(1)'",
                    10,
                )
                harness.runner.wait(run_id)
                _, _, stop_reason = harness._consume_pending_result(state, run_id)
                self.assertIsNone(stop_reason)
            self.assertTrue(state["failure_repair_attempted"])
            state["repair_turn_consumed"] = True
            run_id = harness.runner.submit(
                "failed-repair", root,
                "python -c 'import sys; sys.exit(1)'",
                10,
            )
            harness.runner.wait(run_id)
            _, _, stop_reason = harness._consume_pending_result(state, run_id)
            self.assertEqual(stop_reason, "max_consecutive_failures reached")

    def test_goal_harness_reconciles_historical_terminal_run_count(self):
        from auto_research.goal_harness import GoalHarness

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "research").mkdir()
            harness = GoalHarness(root)
            run_id = harness.runner.submit(
                "idea-count",
                root,
                "python -c 'import json,os; from pathlib import Path; Path(os.environ[\"AUTO_RESEARCH_RUN_DIR\"]).joinpath(\"metrics.json\").write_text(json.dumps({\"score\": 0.8}))'",
                10,
            )
            harness.runner.wait(run_id)
            state = {"completed_runs": 0}
            harness._reconcile_completed_run_count(state)
            self.assertEqual(state["completed_runs"], 1)

    def test_goal_harness_clears_stale_cycle_marker(self):
        from auto_research.goal_harness import GoalHarness

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            research = root / "research"
            research.mkdir()
            marker = research / "active_harness_cycle.json"
            marker.write_text(json.dumps({"cycle_id": "stale", "pid": 99999999}), encoding="utf-8")
            GoalHarness(root)._clear_stale_harness_cycle()
            self.assertFalse(marker.exists())

    def test_goal_harness_checks_conditional_hard_requirements(self):
        from auto_research.goal_harness import GoalHarness

        goal = GoalSpec(
            goal_id="goal-hard-metrics",
            statement="maximize quality",
            primary_metric="ap",
            hard_requirements=[
                {"metric": "ap", "operator": ">=", "value": 0.5},
                {"metric": "recall", "operator": ">=", "value": 0.8, "when": {"thr": 0.1}},
            ],
        )
        result = type("Result", (), {
            "status": "COMPLETED",
            "metrics": {"metrics": {"ap": 0.56, "recall": 0.75}, "params": {"thr": 0.1}},
        })()
        failures = GoalHarness._check_hard_requirements(result, goal)
        self.assertEqual(failures, ["recall >= 0.8 (actual=0.75)"])

    def test_goal_contract_is_active_source_for_hard_requirements(self):
        from auto_research.goal_harness import GoalHarness

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "goal.json").write_text(json.dumps({
                "goal_id": "goal-input",
                "statement": "maximize ap",
                "primary_metric": "ap",
                "hard_requirements": [{"metric": "ap", "operator": ">=", "value": 0.5}],
            }))
            contract_dir = root / "research"
            contract_dir.mkdir()
            (contract_dir / "goal_contract.json").write_text(json.dumps({
                "schema_version": 1,
                "revision": 1,
                "goal_id": "goal-contract",
                "statement": "maximize ap under validated conditions",
                "primary_metric": "ap",
                "hard_requirements": [{"metric": "ap", "operator": ">=", "value": 0.8}],
            }))
            harness = GoalHarness(root)
            self.assertEqual(harness._goal_spec().hard_requirements[0]["value"], 0.8)

    def test_malformed_goal_contract_is_recorded_for_codex_repair(self):
        from auto_research.goal_harness import GoalHarness

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            research = root / "research"
            research.mkdir()
            (research / "goal_contract.json").write_text(
                '{"schema_version": 1, "revision": 1, "goal_id": "broken", '
                '"statement": "bad", "primary_metric": "accuracy", '
                '"hard_requirements": [{"operator": ">=", "value": 0.9}]}',
                encoding="utf-8",
            )
            harness = GoalHarness(root)
            self.assertIsNone(harness._goal_spec())
            error = json.loads((research / "goal_contract_error.json").read_text(encoding="utf-8"))
            self.assertIn("goal_contract.json", error["path"])
            self.assertIn("metric", error["error"])

    def test_malformed_goal_decision_is_recorded_for_codex_repair(self):
        from auto_research.goal_harness import GoalHarness

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            research = root / "research"
            research.mkdir()
            (research / "goal_decision.json").write_text('{"status":"complete"}', encoding="utf-8")
            harness = GoalHarness(root)
            self.assertIsNone(harness._read_goal_decision())
            error = json.loads((research / "goal_decision_error.json").read_text(encoding="utf-8"))
            self.assertIn("decision", error["error"])

    def test_structured_goal_decision_is_required_and_validated(self):
        from auto_research.goal_harness import GoalHarness

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            research = root / "research"
            research.mkdir()
            decision = {
                "status": "complete",
                "decision": "plateau",
                "evidence_run_ids": ["run-example"],
                "hard_requirements_passed": False,
                "reason": "No further evidence value",
            }
            (research / "goal_decision.json").write_text(json.dumps(decision), encoding="utf-8")
            harness = GoalHarness(root)
            self.assertEqual(harness._read_goal_decision(), decision)

            decision["decision"] = "achieved"
            (research / "goal_decision.json").write_text(json.dumps(decision), encoding="utf-8")
            self.assertIsNone(harness._read_goal_decision())
            self.assertIn("hard_requirements_passed", harness._decision_error["error"])


if __name__ == "__main__":
    unittest.main()

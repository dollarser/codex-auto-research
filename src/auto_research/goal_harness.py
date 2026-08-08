"""Event-driven controller for a persistent Codex Goal thread.

The harness never asks Codex whether a run is finished.  It waits locally for
the terminal event, then starts exactly one follow-up turn on the same thread.
"""

from __future__ import annotations

import json
import hashlib
import io
import os
import selectors
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_REASONING_EFFORT,
    HarnessConfig,
    load_harness_config,
)
from .ledger import read_json, write_json_atomic
from .models import GoalSpec
from .runner import ExperimentRunner


class AppServerConnectionError(RuntimeError):
    """The App Server process or stdio connection disappeared."""


class AppServerTimeoutError(AppServerConnectionError):
    """The App Server kept the connection open but stopped producing data."""

    def __init__(self, message: str, *, context: str = "", stderr_tail: str = "", returncode: int | None = None):
        super().__init__(message)
        self.context = context
        self.stderr_tail = stderr_tail
        self.returncode = returncode


class AppServerTurnTimeout(AppServerTimeoutError):
    """A turn exceeded the Harness watchdog and was interrupted or abandoned."""

    def __init__(self, message: str, turn_id: str | None = None):
        super().__init__(message)
        self.turn_id = turn_id


def _extract_run_ids(value: Any) -> set[str]:
    """Extract run ids from App Server notifications and nested MCP payloads."""
    found: set[str] = set()
    if isinstance(value, dict):
        run_id = value.get("run_id")
        if isinstance(run_id, str):
            found.add(run_id)
        for item in value.values():
            found.update(_extract_run_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_extract_run_ids(item))
    elif isinstance(value, str) and "run_id" in value:
        try:
            found.update(_extract_run_ids(json.loads(value)))
        except json.JSONDecodeError:
            pass
    return found


def _event_turn_id(message: dict[str, Any]) -> str | None:
    """Read the turn id from App Server lifecycle/item notifications."""
    params = message.get("params", {})
    if not isinstance(params, dict):
        return None
    turn_id = params.get("turnId")
    if isinstance(turn_id, str):
        return turn_id
    turn = params.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return turn["id"]
    return None


@contextmanager
def _exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    import fcntl
    with path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class AppServerClient:
    """Small JSONL client for the local Codex App Server protocol."""

    def __init__(self, cwd: str | Path, config: HarnessConfig | None = None):
        self.cwd = str(Path(cwd).resolve())
        self.config = config or load_harness_config(self.cwd)
        self.sandbox = self.config.codex_sandbox
        self.approval_policy = self.config.codex_approval
        self.model = self.config.codex_model
        self.reasoning_effort = self.config.codex_reasoning_effort
        self._spawn()

    def _model_overrides(self) -> dict[str, str]:
        """Return explicit App Server model controls, omitting unset values."""
        overrides: dict[str, str] = {}
        if self.model:
            overrides["model"] = self.model
        if self.reasoning_effort:
            overrides["effort"] = self.reasoning_effort
        return overrides

    def model_settings(self) -> dict[str, str | None]:
        """Expose the effective Harness settings for durable provenance."""
        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }

    def _spawn(self) -> None:
        self._stderr_lines = deque(maxlen=200)
        # App Server may deliver notifications before the response to the
        # request that caused them. Never discard such lifecycle events while
        # waiting for a JSON-RPC response.
        self._pending_messages: deque[dict[str, Any]] = deque()
        self.process = subprocess.Popen(
            ["codex", "app-server", "--stdio"],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        self._next_id = 1
        self.last_thread_id: str | None = None
        self.last_turn_id: str | None = None
        self._active_thread_id: str | None = None
        self._active_turn_id: str | None = None

    def _drain_stderr(self) -> None:
        if not self.process.stderr:
            return
        for line in self.process.stderr:
            self._stderr_lines.append(line.rstrip())

    def _stderr_tail(self) -> str:
        return "\n".join(self._stderr_lines)[-4000:]

    def _readline_with_timeout(self, timeout_s: float, context: str) -> str:
        """Read one JSONL line without allowing a silent App Server to hang us."""
        if not self.process.stdout:
            raise AppServerConnectionError("App Server stdout is closed")
        stream = self.process.stdout
        try:
            fd = stream.fileno()
        except (AttributeError, io.UnsupportedOperation, OSError, ValueError):
            # In-memory streams used by unit tests do not expose a file
            # descriptor. Production App Server stdout always does.
            return stream.readline()
        selector = selectors.DefaultSelector()
        try:
            selector.register(fd, selectors.EVENT_READ)
            if not selector.select(max(0.0, timeout_s)):
                stderr_tail = self._stderr_tail()
                detail = f"; stderr={stderr_tail[-1000:]}" if stderr_tail else ""
                raise AppServerTimeoutError(
                    f"App Server produced no data for {context} within {timeout_s:.1f}s{detail}",
                    context=context,
                    stderr_tail=stderr_tail,
                    returncode=self.process.poll() if hasattr(self.process, "poll") else None,
                )
            line = stream.readline()
        finally:
            selector.close()
        if not line:
            error = self._stderr_tail()
            raise AppServerConnectionError(f"App Server stdout closed during {context}: {error}")
        return line

    def _send(self, method: str, params: dict[str, Any]) -> int:
        if not self.process.stdin:
            raise AppServerConnectionError("App Server stdin is closed")
        request_id = self._next_id
        self._next_id += 1
        self.process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        return request_id

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.process.stdin:
            raise AppServerConnectionError("App Server stdin is closed")
        self.process.stdin.write(json.dumps({"method": method, "params": params}) + "\n")
        self.process.stdin.flush()

    def _send_result(self, request_id: Any, result: dict[str, Any]) -> None:
        if not self.process.stdin:
            raise AppServerConnectionError("App Server stdin is closed")
        self.process.stdin.write(json.dumps({"id": request_id, "result": result}) + "\n")
        self.process.stdin.flush()

    def _handle_server_request(self, message: dict[str, Any]) -> bool:
        """Answer non-interactive server requests so they cannot stall a turn."""
        request_id = message.get("id")
        method = message.get("method")
        if request_id is None or not isinstance(method, str):
            return False
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "applyPatchApproval",
            "item/permissions/requestApproval",
        }:
            # This Harness runs with explicit non-interactive policy. A
            # request that still reaches us must not wait for a human prompt.
            result = {"decision": "decline"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "cancel"}
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        else:
            if not self.process.stdin:
                raise AppServerConnectionError("App Server stdin is closed")
            self.process.stdin.write(json.dumps({
                "id": request_id,
                "error": {"code": -32601, "message": f"unsupported server request: {method}"},
            }) + "\n")
            self.process.stdin.flush()
            return True
        self._send_result(request_id, result)
        return True

    def _read_response(self, request_id: int, timeout_s: float | None = None) -> dict[str, Any]:
        timeout_s = timeout_s or self.config.app_server_response_timeout_s
        if not hasattr(self, "_pending_messages"):
            self._pending_messages = deque()
        deferred: list[dict[str, Any]] = []
        while True:
            if self._pending_messages:
                message = self._pending_messages.popleft()
            else:
                line = self._readline_with_timeout(timeout_s, f"response {request_id}")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AppServerConnectionError(f"Invalid App Server JSONL response: {line[:500]!r}") from exc
            if self._handle_server_request(message):
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                self._pending_messages.extend(deferred)
                return message
            deferred.append(message)

    def initialize(self) -> None:
        request_id = self._send(
            "initialize",
            {"clientInfo": {"name": "auto-research-goal-harness", "version": "0.1.0"}},
        )
        self._read_response(request_id)
        self._notify("initialized", {})

    def start_thread(self, objective: str) -> str:
        request_id = self._send(
            "thread/start",
            {
                "cwd": self.cwd,
                "approvalPolicy": self.approval_policy,
                "sandbox": self.sandbox,
                **self._model_overrides(),
            },
        )
        response = self._read_response(request_id)
        thread_id = response["result"]["thread"]["id"]
        # Persist this as soon as App Server creates it. If thread/goal/set
        # stalls, the caller can still resume the durable thread.
        self.last_thread_id = thread_id
        self.set_goal(thread_id, objective)
        return thread_id

    def resume_thread(self, thread_id: str) -> None:
        request_id = self._send("thread/resume", {"threadId": thread_id})
        self._read_response(request_id)
        self.last_thread_id = thread_id

    def reconnect(self, thread_id: str) -> None:
        """Recreate the process and attach to an existing persisted thread."""
        self.close()
        self._next_id = 1
        self._spawn()
        self.initialize()
        self.resume_thread(thread_id)

    def set_goal(self, thread_id: str, objective: str) -> None:
        request_id = self._send(
            "thread/goal/set",
            {"threadId": thread_id, "objective": objective, "status": "active"},
        )
        self._read_response(request_id)

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        """Best-effort interruption for a turn that stopped producing events."""
        request_id = self._send("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        self._read_response(request_id, timeout_s=min(self.config.app_server_response_timeout_s, 10.0))

    def _mark_turn_finished(self, thread_id: str, turn_id: str, reason: str) -> None:
        if self._active_thread_id == thread_id and self._active_turn_id == turn_id:
            self._active_thread_id = None
            self._active_turn_id = None
        callback = getattr(self, "on_turn_finished", None)
        if callable(callback):
            callback(turn_id, reason)

    def _interrupt_active_turn(self, thread_id: str, turn_id: str, reason: str) -> None:
        """Close a durable submission/decision turn before releasing App Server."""
        try:
            self.interrupt_turn(thread_id, turn_id)
        except (AppServerConnectionError, AppServerTimeoutError, RuntimeError, OSError):
            # The turn may have completed between the durable file write and
            # this request. The durable boundary is still authoritative.
            pass
        finally:
            self._mark_turn_finished(thread_id, turn_id, reason)

    def start_turn(self, thread_id: str, prompt: str) -> set[str]:
        if not hasattr(self, "_pending_messages"):
            self._pending_messages = deque()
        turn_deadline = time.monotonic() + self.config.app_server_turn_timeout_s
        request_id = self._send(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "cwd": self.cwd,
                "approvalPolicy": self.approval_policy,
                "sandboxPolicy": {"type": "dangerFullAccess" if self.sandbox == "danger-full-access" else "workspaceWrite"},
                **self._model_overrides(),
            },
        )
        response = self._read_response(
            request_id,
            timeout_s=min(self.config.app_server_response_timeout_s, self.config.app_server_turn_timeout_s),
        )
        turn = response.get("result", {}).get("turn", {})
        active_turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(active_turn_id, str) or not active_turn_id:
            raise AppServerConnectionError("turn/start response did not contain a turn id")
        self.last_turn_id = active_turn_id
        self._active_thread_id = thread_id
        self._active_turn_id = active_turn_id
        callback = getattr(self, "on_turn_started", None)
        if callable(callback):
            callback(active_turn_id)
        run_ids: set[str] = set()
        event_idle_timeout = min(
            getattr(self.config, "app_server_event_idle_timeout_s", 180.0),
            self.config.app_server_turn_timeout_s,
        )
        if not self.process.stdout:
            raise AppServerConnectionError("App Server stdout is closed")
        while True:
            remaining = turn_deadline - time.monotonic()
            if remaining <= 0:
                self._interrupt_active_turn(thread_id, active_turn_id, "turn_timeout")
                raise AppServerTurnTimeout(
                    f"turn {active_turn_id} exceeded {self.config.app_server_turn_timeout_s:.1f}s; "
                    "the turn was interrupted or the App Server must be restarted",
                    turn_id=active_turn_id,
                )
            try:
                completion_probe = getattr(self, "completion_probe", None)
                if callable(completion_probe) and completion_probe():
                    self._interrupt_active_turn(thread_id, active_turn_id, "decision_persisted")
                    return run_ids
                run_probe = getattr(self, "run_probe", None)
                if callable(run_probe):
                    discovered_runs = run_probe()
                    if discovered_runs:
                        self._interrupt_active_turn(thread_id, active_turn_id, "experiment_persisted")
                        return set(discovered_runs) | run_ids
                if self._pending_messages:
                    message = self._pending_messages.popleft()
                else:
                    line = self._readline_with_timeout(
                        min(remaining, event_idle_timeout),
                        f"turn {active_turn_id} (idle event watchdog)",
                    )
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise AppServerConnectionError(f"Invalid App Server JSONL event: {line[:500]!r}") from exc
            except AppServerTimeoutError as exc:
                self._interrupt_active_turn(thread_id, active_turn_id, "event_idle_timeout")
                raise AppServerTurnTimeout(str(exc), turn_id=active_turn_id) from exc
            if self._handle_server_request(message):
                continue
            # Only trust MCP tool results. Shell output and model text may
            # mention historical run_ids while inspecting the ledger.
            message_turn_id = _event_turn_id(message)
            if message.get("method") == "item/completed" and message_turn_id == active_turn_id:
                item = message.get("params", {}).get("item", {})
                if item.get("type") == "mcpToolCall":
                    run_ids.update(_extract_run_ids(item))
                    structured = item.get("result", {}).get("structuredContent", {})
                    if (
                        run_ids
                        and isinstance(structured, dict)
                        and structured.get("status") == "RUNNING"
                        and isinstance(structured.get("run_id"), str)
                    ):
                        # A durable start_experiment result is sufficient to
                        # close the submission turn when App Server omits the
                        # later turn/completed notification.
                        self._interrupt_active_turn(thread_id, active_turn_id, "experiment_persisted")
                        return run_ids
                # Some App Server builds persist task_complete/final_answer
                # but omit the wire-level turn/completed notification. A
                # final agent message is the terminal item for that turn; use
                # it as a conservative lifecycle fallback so the Harness does
                # not wait until the 900s watchdog.
                if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                    self._mark_turn_finished(thread_id, active_turn_id, "final_answer")
                    return run_ids
            if message.get("method") == "turn/completed" and message_turn_id == active_turn_id:
                self._mark_turn_finished(thread_id, active_turn_id, "turn_completed")
                return run_ids

    def close(self) -> None:
        if self.process.poll() is None:
            active_thread_id = getattr(self, "_active_thread_id", None)
            active_turn_id = getattr(self, "_active_turn_id", None)
            if active_thread_id and active_turn_id:
                self._interrupt_active_turn(active_thread_id, active_turn_id, "client_closing")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


class GoalHarness:
    DECISION_TYPES = frozenset({"achieved", "plateau", "budget_exhausted", "blocked"})

    """Resume a Goal only after a local terminal event, once per run."""

    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir).resolve()
        self.config = load_harness_config(self.project_dir)
        self.runs_dir = self.project_dir / "research" / "runs"
        self.state_path = self.project_dir / "research" / "goal_harness.json"
        self.contract_error_path = self.project_dir / "research" / "goal_contract_error.json"
        self.decision_error_path = self.project_dir / "research" / "goal_decision_error.json"
        self.harness_cycle_path = self.project_dir / "research" / "active_harness_cycle.json"
        self._goal_error: dict[str, str] | None = None
        self._decision_error: dict[str, str] | None = None
        self.runner = ExperimentRunner(self.runs_dir, config=self.config)

    def _state(self) -> dict[str, Any]:
        return read_json(self.state_path, {}) or {}

    def _save(self, state: dict[str, Any]) -> None:
        write_json_atomic(self.state_path, state)

    def _wait_for_event(self, run_id: str):
        # This wait is local to the harness. It does not create a Codex turn or
        # call an MCP status tool. The durable run remains recoverable if this
        # process is restarted before the event arrives.
        return self.runner.wait(run_id, poll_s=self.config.event_poll_s, grace_s=self.config.event_grace_s)

    def _run_ids_on_disk(self) -> set[str]:
        result: set[str] = set()
        for run_file in self.runs_dir.glob("run-*/run.json"):
            run = read_json(run_file, {})
            if run.get("run_id"):
                result.add(run["run_id"])
        return result

    def _reconcile_completed_run_count(self, state: dict[str, Any]) -> None:
        """Recover the total terminal-run count after a controller interruption."""
        completed = 0
        for run_file in self.runs_dir.glob("run-*/run.json"):
            run = read_json(run_file, {}) or {}
            run_id = run.get("run_id")
            if not isinstance(run_id, str):
                continue
            try:
                if self.runner.get_result(run_id) is not None:
                    completed += 1
            except (FileNotFoundError, ValueError):
                continue
        # The run ledger is authoritative. A monotonic-only update caused a
        # resumed task to retain counts from a different historical session.
        state["completed_runs"] = completed
        state["reconciled_at"] = time.time()

    def _open_harness_cycle(self, cycle: int, thread_id: str) -> str:
        cycle_id = f"cycle-{cycle}-{uuid.uuid4().hex}"
        write_json_atomic(self.harness_cycle_path, {
            "cycle_id": cycle_id,
            "cycle": cycle,
            "thread_id": thread_id,
            "pid": os.getpid(),
            "created_at": time.time(),
        })
        return cycle_id

    def _close_harness_cycle(self, cycle_id: str | None = None) -> None:
        marker = read_json(self.harness_cycle_path, {}) or {}
        if cycle_id is None or marker.get("cycle_id") == cycle_id:
            self.harness_cycle_path.unlink(missing_ok=True)

    def _clear_stale_harness_cycle(self) -> None:
        """Remove a cycle marker whose Harness process is no longer alive."""
        marker = read_json(self.harness_cycle_path, {}) or {}
        pid = marker.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            self.harness_cycle_path.unlink(missing_ok=True)
            return
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            self.harness_cycle_path.unlink(missing_ok=True)

    def _record_goal_error(self, path: Path, error: Exception) -> None:
        self._goal_error = {"path": str(path), "error": str(error)}
        write_json_atomic(self.contract_error_path, self._goal_error)

    def _read_goal_file(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not value:
                raise ValueError("goal file must contain a non-empty JSON object")
            return value
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._record_goal_error(path, exc)
            return None

    def _start_turn_with_reconnect(self, client: AppServerClient, thread_id: str, prompt: str) -> set[str]:
        known_runs = self._run_ids_on_disk()
        last_error: Exception | None = None
        for attempt in range(client.config.reconnect_attempts):
            try:
                client.run_probe = lambda: self._run_ids_on_disk() - known_runs
                return client.start_turn(thread_id, prompt)
            except AppServerConnectionError as exc:
                last_error = exc
                discovered = self._run_ids_on_disk() - known_runs
                if len(discovered) == 1:
                    # The MCP call was durable even though its response stream
                    # was lost. Do not submit a duplicate turn.
                    return discovered
                if isinstance(exc, AppServerTimeoutError):
                    # A silent server is not equivalent to a broken pipe.
                    # Retrying turn/start could create a duplicate turn after
                    # the original server eventually resumes.
                    raise
                if attempt == client.config.reconnect_attempts - 1:
                    break
                time.sleep(client.config.reconnect_backoff_s**attempt)
                client.reconnect(thread_id)
                prompt = (
                    "上一个 App Server 连接中断。请检查 research/runs 中是否已有本轮实验；"
                    "若已有 RUNNING 实验，不要重复提交，直接结束本 turn；"
                    "若没有，则继续当前研究并只调用一次 start_experiment。\n" + prompt
                )
        raise AppServerConnectionError(f"could not recover App Server turn: {last_error}")

    def _goal_spec(self) -> GoalSpec | None:
        self._goal_error = None
        for path in (
            self.project_dir / "research" / "goal_contract.json",
            self.project_dir / "goal.json",
        ):
            data = self._read_goal_file(path)
            if data is None:
                if path.exists():
                    return None
                continue
            try:
                if path.name == "goal_contract.json":
                    if data.get("schema_version") != 1:
                        raise ValueError("goal_contract.json must declare schema_version=1")
                    if not isinstance(data.get("revision"), int) or data["revision"] < 1:
                        raise ValueError("goal_contract.json must declare a positive integer revision")
                goal = GoalSpec.from_dict(data)
                self.contract_error_path.unlink(missing_ok=True)
                return goal
            except (KeyError, TypeError, ValueError) as exc:
                self._record_goal_error(path, exc)
                return None
        return None

    def _operator_limits(self) -> GoalSpec | None:
        """Read immutable execution limits from the operator-provided goal."""
        path = self.project_dir / "goal.json"
        data = self._read_goal_file(path)
        if data:
            try:
                return GoalSpec.from_dict(data)
            except (KeyError, TypeError, ValueError) as exc:
                self._record_goal_error(path, exc)
                return None
        return self._goal_spec()

    def _goal_contract_path(self) -> Path:
        return self.project_dir / "research" / "goal_contract.json"

    def _goal_decision_path(self) -> Path:
        return self.project_dir / "research" / "goal_decision.json"

    def _read_goal_decision(self) -> dict[str, Any] | None:
        self._decision_error = None
        path = self._goal_decision_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("goal_decision.json must contain a JSON object")
            if data.get("status") != "complete":
                raise ValueError("goal_decision.json status must be 'complete'")
            if data.get("decision") not in self.DECISION_TYPES:
                raise ValueError("goal_decision.json decision is invalid")
            evidence = data.get("evidence_run_ids")
            if not isinstance(evidence, list) or not evidence or not all(
                isinstance(run_id, str) and run_id for run_id in evidence
            ):
                raise ValueError("goal_decision.json evidence_run_ids must be a non-empty string array")
            if not isinstance(data.get("hard_requirements_passed"), bool):
                raise ValueError("goal_decision.json hard_requirements_passed must be boolean")
            if data["decision"] == "achieved" and not data["hard_requirements_passed"]:
                raise ValueError("decision=achieved requires hard_requirements_passed=true")
            if not isinstance(data.get("reason"), str) or not data["reason"].strip():
                raise ValueError("goal_decision.json reason must be a non-empty string")
            self.decision_error_path.unlink(missing_ok=True)
            return data
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._decision_error = {"path": str(path), "error": str(exc)}
            write_json_atomic(self.decision_error_path, self._decision_error)
            return None

    def _goal_decision_repair_prompt(self, objective: str, user_prompt: str) -> str:
        error = json.dumps(self._decision_error or {}, ensure_ascii=False)
        return (
            "Codex 的 goal_decision.json 格式无效，Harness 已捕获异常并暂停后续实验。"
            f"错误上下文：{error}\n"
            "请修复 research/goal_decision.json：如果研究已经完成，写入 "
            "{\"status\":\"complete\",\"decision\":\"plateau\","
            "\"evidence_run_ids\":[\"run-...\"],\"hard_requirements_passed\":false,"
            "\"reason\":\"...\"}；如果仍需实验，删除该文件并结束本 turn，"
            "不要在本 turn 启动实验。不要修改 goal.json、goal_contract.json 或历史记录。"
            f"\n当前目标：{objective}\n补充说明：{user_prompt}"
        )

    def _contract_digest(self) -> str:
        path = self._goal_contract_path()
        if not path.exists():
            return ""
        data = self._read_goal_file(path)
        if data is None:
            return ""
        canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _goal_objective(goal: GoalSpec) -> str:
        objective = (
            f"{goal.statement}\n"
            f"当前主指标：{goal.primary_metric}（{goal.direction}）。"
            "详细目标契约见 research/goal_contract.json。"
        )
        return objective[:4000]

    def _sync_goal_if_contract_changed(
        self,
        client: AppServerClient,
        thread_id: str,
        previous_digest: str,
    ) -> str:
        current_digest = self._contract_digest()
        if current_digest and current_digest != previous_digest:
            goal = self._goal_spec()
            if goal:
                client.set_goal(thread_id, self._goal_objective(goal))
        return current_digest

    def _goal_refinement_prompt(self, objective: str, user_prompt: str) -> str:
        return (
            "在开始任何实验前，先主动审查并优化用户提出的研究目标。"
            "不要把用户原话、原始 target_metric 或次要指标直接当作最终验收标准。"
            "请检查目标是否可测量、是否与真实业务问题相关、指标是否重要、评估协议是否存在数据泄漏，"
            "并区分 hard_requirements（只能是带 metric/operator/value 的数值门槛）、protocol_requirements（自然语言协议约束）与 soft_preferences。读取 baseline、评估代码、历史 ledger 和必要的研究证据。\n"
            f"用户原始目标：{objective}\n"
            f"用户补充说明：{user_prompt}\n"
            "请把优化后的目标契约写入 research/goal_contract.json。JSON 至少包含："
            "schema_version=1、revision=1、goal_id、statement、primary_metric、direction、secondary_metrics、baseline、"
            "search_space、constraints、stopping、hard_requirements、protocol_requirements、soft_preferences、"
            "rejected_requirements、revision 和 reasoning_summary。"
            "该文件中的 primary_metric 必须能由 metrics.json 验证。"
            "本 turn 只做目标澄清、证据检查和契约落盘；不要修改算法代码，不要启动实验。"
        )

    def _goal_repair_prompt(self, objective: str, user_prompt: str) -> str:
        error = json.dumps(self._goal_error or {}, ensure_ascii=False)
        return (
            "研究契约文件格式无效，Harness 已捕获异常并暂停启动实验。"
            f"错误上下文：{error}\n"
            "请读取错误文件和相关 goal 文件，修复 research/goal_contract.json。"
            "只修复 schema、字段类型和内容一致性，不要修改 goal.json、baseline、评估协议或历史实验记录。"
            "hard_requirements 只能是包含 metric/operator/value 的数值门槛对象；"
            "自然语言协议约束必须放入 protocol_requirements 字符串数组。"
            "修复后重新读取并验证 JSON，确认 primary_metric、direction、stopping、search_space 等字段完整。"
            "本 turn 不要修改算法代码，不要启动实验。"
            f"用户原始目标：{objective}\n用户补充说明：{user_prompt}"
        )

    @staticmethod
    def _experiment_prompt() -> str:
        return (
            "目标契约已生成。请读取 research/goal_contract.json，并以它作为当前研究目标，"
            "而不是机械执行用户原始要求。提出多个可证伪 idea，选择最有价值的一个，"
            "只修改允许的 worktree 文件并做 smoke test，然后只调用一次 start_experiment。"
            "不要在本 turn 内等待实验，不要查询 RUNNING 状态。"
        )

    @staticmethod
    def _result_prompt(
        run_id: str,
        hard_failures: list[str] | None = None,
        result: Any | None = None,
        force_repair: bool = False,
    ) -> str:
        hard_feedback = ""
        if hard_failures:
            hard_feedback = (
                "Harness 已发现以下不可放宽的硬指标未满足："
                + json.dumps(hard_failures, ensure_ascii=False)
                + "。该实验不能作为满足硬约束的结果晋级。\n"
            )
        diagnostics = ""
        if result is not None and getattr(result, "status", "") != "COMPLETED":
            diagnostics = (
                "实验执行失败。请先读取并分析以下诊断信息以及 run 目录中的完整日志，"
                "不要只把退出码当作算法结论：\n"
                + json.dumps({
                    "status": result.status,
                    "return_code": result.return_code,
                    "error": result.error,
                    "command": getattr(result, "command", ""),
                    "argv": getattr(result, "argv", []),
                    "worktree": getattr(result, "worktree", ""),
                    "stderr_tail": getattr(result, "stderr_tail", ""),
                    "stdout_tail": getattr(result, "stdout_tail", ""),
                    "result_dir": result.result_dir,
                }, ensure_ascii=False)
                + "\n"
            )
        repair_instruction = ""
        if force_repair:
            repair_instruction = (
                "这是达到连续失败保护阈值前的最后一次 repair turn。必须先定位并修复失败根因"
                "（包括解释器、依赖、工作目录、命令、权限或实验代码），做最小 smoke test，"
                "再只启动一个修复后的实验；不得通过放宽目标、删除指标或伪造 metrics 绕过故障。\n"
            )
        elif diagnostics:
            repair_instruction = (
                "如果这是可修复的执行/环境故障，先修复根因并验证，再启动一个新实验；"
                "不要把环境故障当成算法 idea 失败。\n"
            )
        return (
            f"实验终态事件已到达，run_id={run_id}。"
            "请只调用一次 get_experiment_result(run_id) 读取完整结果，分析 promote/discard/replicate/repair。"
            + hard_feedback + diagnostics + repair_instruction
            + "默认保持当前 research/goal_contract.json 不变，不要因为每轮实验结束就重新定义目标。"
            + "只有当实验反馈明确证明当前研究问题、指标或约束不合理、不可测量或与目标明显错位时，才修订 contract。"
            + "如果确实修订 hard_requirements，必须把修改原因、旧值、新值和证据写入 goal_contract.json 的 revision 或 ledger。"
            + "如果当前目标已经达成，或继续实验没有足够价值，不要启动新实验；请写入 "
            + "research/goal_decision.json，必须包含 status=complete、decision（achieved/plateau/"
            + "budget_exhausted/blocked）、evidence_run_ids（至少一个真实 run_id）、"
            + "hard_requirements_passed（布尔值）和 reason。"
            + "如果仍有研究价值，则选择一个新 idea 并只调用一次 start_experiment，然后结束 turn。"
        )

    @staticmethod
    def _metric_value(metrics: dict[str, Any], name: str) -> Any:
        if name in metrics:
            return metrics[name]
        nested = metrics.get("metrics")
        if isinstance(nested, dict) and name in nested:
            return nested[name]
        return None

    @staticmethod
    def _context_value(metrics: dict[str, Any], name: str) -> Any:
        for key in ("params", "context", "config", "conditions"):
            values = metrics.get(key)
            if isinstance(values, dict) and name in values:
                return values[name]
        return metrics.get(name)

    @staticmethod
    def _compare(actual: Any, operator: str, expected: Any) -> bool:
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False
        if not isinstance(expected, (int, float)) or isinstance(expected, bool):
            return False
        return {
            ">": actual > expected,
            ">=": actual >= expected,
            "<": actual < expected,
            "<=": actual <= expected,
            "==": actual == expected,
            "!=": actual != expected,
        }.get(operator, False)

    @classmethod
    def _check_hard_requirements(
        cls,
        result: Any,
        goal: GoalSpec | None,
        requirements: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        if goal is None or result.status != "COMPLETED":
            return []
        failures: list[str] = []
        for requirement in requirements if requirements is not None else goal.hard_requirements:
            metric = requirement.get("metric")
            operator = requirement.get("operator", requirement.get("op", ">="))
            expected = requirement.get("value")
            if not isinstance(metric, str) or not isinstance(operator, str):
                failures.append(f"invalid hard requirement: {requirement!r}")
                continue
            condition = requirement.get("when", {})
            if isinstance(condition, dict):
                condition_failed = False
                for name, condition_value in condition.items():
                    condition_operator = "=="
                    expected_condition = condition_value
                    if isinstance(condition_value, dict):
                        condition_operator = condition_value.get("operator", condition_value.get("op", "=="))
                        expected_condition = condition_value.get("value")
                    actual_condition = cls._context_value(result.metrics, name)
                    if not cls._compare(actual_condition, condition_operator, expected_condition):
                        condition_failed = True
                        break
                if condition_failed:
                    continue
            actual = cls._metric_value(result.metrics, metric)
            if not cls._compare(actual, operator, expected):
                failures.append(f"{metric} {operator} {expected} (actual={actual!r})")
        return failures

    @staticmethod
    def _should_stop(
        state: dict[str, Any],
        result: Any,
        goal: GoalSpec | None,
        limits: GoalSpec | None = None,
    ) -> str | None:
        limits = limits or goal
        if limits is None:
            return None
        if state.get("completed_runs", 0) >= limits.max_experiments:
            return "max_experiments reached"
        if state.get("consecutive_failures", 0) >= limits.max_consecutive_failures:
            return "max_consecutive_failures reached"
        return None

    def _consume_pending_result(self, state: dict[str, Any], pending_run_id: str) -> tuple[Any, list[str], str | None]:
        """Consume a terminal run and persist its metrics exactly once."""
        result = self._wait_for_event(pending_run_id)
        goal = self._goal_spec()
        run_metadata = self.runner.get_run(pending_run_id)
        value = self._metric_value(result.metrics, goal.primary_metric) if goal else None
        valid_metric = isinstance(value, (int, float))
        hard_requirements = run_metadata.get("hard_requirements_snapshot")
        hard_failures = self._check_hard_requirements(result, goal, hard_requirements)
        execution_failed = result.status != "COMPLETED" or not valid_metric
        failures = state.get("consecutive_failures", 0)
        failures = failures + 1 if execution_failed else 0
        hard_gate_failures = state.get("consecutive_hard_gate_failures", 0)
        hard_gate_failures = hard_gate_failures + 1 if hard_failures else 0
        recent_metrics = list(state.get("recent_metrics", []))
        if valid_metric:
            recent_metrics.append(float(value))
            if goal and goal.plateau_window > 0:
                recent_metrics = recent_metrics[-goal.plateau_window:]
        best_metric = state.get("best_metric")
        if valid_metric and (best_metric is None or (
                goal and ((goal.direction == "maximize" and value > best_metric) or
                          (goal.direction == "minimize" and value < best_metric)))):
            best_metric = float(value)
        state.update({
            "phase": "RESULT_READY",
            "last_result": result.to_dict(),
            "completed_runs": state.get("completed_runs", 0) + 1,
            "consecutive_failures": failures,
            "consecutive_hard_gate_failures": hard_gate_failures,
            "last_metric": float(value) if valid_metric else None,
            "best_metric": best_metric,
            "recent_metrics": recent_metrics,
            "hard_requirements_passed": not hard_failures,
            "hard_requirement_failures": hard_failures,
            "pending_run_id": None,
        })
        if execution_failed:
            limits = self._operator_limits()
            threshold = limits.max_consecutive_failures if limits else 0
            repair_already_attempted = bool(state.get("failure_repair_attempted", False))
            if threshold and failures >= threshold and not repair_already_attempted:
                # Give Codex one explicit repair turn before the safety stop.
                state["failure_repair_attempted"] = True
                state["repair_turn_consumed"] = False
            elif repair_already_attempted:
                state["failure_repair_attempted"] = True
        else:
            state["failure_repair_attempted"] = False
            state["repair_turn_consumed"] = False
        self._save(state)
        stop_reason = self._should_stop(state, result, goal, self._operator_limits())
        if execution_failed and state.get("failure_repair_attempted") and not state.get("repair_turn_consumed"):
            # The first threshold crossing is recoverable: let the next Goal
            # turn repair the execution environment before enforcing stop.
            stop_reason = None
        return result, hard_failures, stop_reason

    def run(
        self,
        objective: str,
        prompt: str,
        max_cycles: int | None = None,
        fresh_thread: bool = False,
    ) -> dict[str, Any]:
        max_cycles = max_cycles or self.config.max_cycles
        self._clear_stale_harness_cycle()
        state = self._state()
        self._reconcile_completed_run_count(state)
        # Do not present diagnostics from an older App Server process as if
        # they belonged to this invocation. Keep last_result/metrics as
        # research history, but reset live startup/turn fields immediately.
        state.update({
            "phase": "STARTING",
            "reason": "",
            "stop_reason": None,
            "turn_id": None,
            "turn_started_at": None,
            "turn_finished_at": None,
            "turn_status": None,
            "app_server_context": "",
            "app_server_stderr": "",
            "app_server_returncode": None,
        })
        self._save(state)
        goal = self._goal_spec()
        operator_limits = self._operator_limits()
        lock = _exclusive_file_lock(self.project_dir / "research" / "goal_harness.lock")
        lock.__enter__()
        client: AppServerClient | None = None
        startup_complete = False
        try:
            # A terminal decision is durable and must be consumed before
            # starting App Server. Otherwise a supervised invocation using
            # --fresh-thread creates a new visible Codex session on every
            # restart even though the research is already complete.
            if not state.get("pending_run_id"):
                decision = self._read_goal_decision()
                if decision and decision.get("status") == "complete":
                    state.update({
                        "phase": "COMPLETED",
                        "stop_reason": decision.get("decision"),
                        "reason": decision.get("reason", "Codex Goal declared the research complete"),
                        "pending_run_id": None,
                    })
                    self._save(state)
                    return state
            client = AppServerClient(self.project_dir)
            client.completion_probe = lambda: self._read_goal_decision() is not None
            def persist_turn_started(turn_id: str) -> None:
                state.update({
                    "turn_id": turn_id,
                    "turn_started_at": time.time(),
                    "turn_finished_at": None,
                    "turn_status": "running",
                })
                self._save(state)
            def persist_turn_finished(turn_id: str, reason: str) -> None:
                if state.get("turn_id") == turn_id:
                    state.update({
                        "turn_finished_at": time.time(),
                        "turn_status": reason,
                    })
                    self._save(state)
            client.on_turn_started = persist_turn_started
            client.on_turn_finished = persist_turn_finished
            client.initialize()
            state.update({
                "codex_model": client.model,
                "codex_reasoning_effort": client.reasoning_effort,
            })
            self._save(state)
            thread_id = None if fresh_thread else state.get("thread_id")
            if thread_id:
                client.resume_thread(thread_id)
                if state.get("goal_sync_pending"):
                    active_goal = self._goal_spec()
                    if active_goal:
                        client.set_goal(thread_id, self._goal_objective(active_goal))
                    state.update({"goal_sync_pending": False})
                    self._save(state)
            else:
                thread_id = client.start_thread(
                    "主动优化以下研究目标后再开始实验；原始描述不是最终验收标准：" + objective
                )
                state = {
                    **state,
                    "thread_id": thread_id,
                    "cycle": 0,
                    "phase": "GOAL_READY" if self._goal_contract_path().exists() else "GOAL_REFINEMENT",
                    "codex_model": client.model,
                    "codex_reasoning_effort": client.reasoning_effort,
                }
                self._save(state)
            startup_complete = True

            pending_run_id = state.get("pending_run_id")
            if self._goal_error:
                current_prompt = self._goal_repair_prompt(objective, prompt)
                state.update({"phase": "GOAL_REPAIR", "goal_contract_error": self._goal_error})
                self._save(state)
            elif state.get("phase") == "GOAL_REFINEMENT" or not self._goal_contract_path().exists():
                current_prompt = self._goal_refinement_prompt(objective, prompt)
            else:
                current_prompt = self._experiment_prompt()
            for cycle in range(max_cycles):
                self._reconcile_completed_run_count(state)
                if pending_run_id:
                    result, hard_failures, stop_reason = self._consume_pending_result(state, pending_run_id)
                    if stop_reason:
                        state.update({
                            "phase": "STOPPED",
                            "stop_reason": stop_reason,
                            "reason": stop_reason,
                        })
                        self._save(state)
                        return state
                    force_repair = bool(
                        state.get("failure_repair_attempted")
                        and not state.get("repair_turn_consumed")
                        and result.status != "COMPLETED"
                    )
                    current_prompt = (
                        self._goal_repair_prompt(objective, prompt)
                        if self._goal_error
                        else self._result_prompt(
                            pending_run_id,
                            hard_failures,
                            result=result,
                            force_repair=force_repair,
                        )
                    )
                    if force_repair:
                        state["repair_turn_consumed"] = True
                        self._save(state)
                    pending_run_id = None

                state.update({"phase": "GOAL_RUNNING", "cycle": cycle + 1})
                self._save(state)
                self._goal_decision_path().unlink(missing_ok=True)
                contract_digest = self._contract_digest()
                cycle_id = self._open_harness_cycle(cycle + 1, thread_id)
                try:
                    run_ids = self._start_turn_with_reconnect(client, thread_id, current_prompt)
                except AppServerTimeoutError as exc:
                    self._reconcile_completed_run_count(state)
                    state.update({
                        "phase": "APP_SERVER_STALLED",
                        "reason": str(exc),
                        "turn_id": getattr(exc, "turn_id", None),
                        "app_server_context": getattr(exc, "context", ""),
                        "app_server_stderr": getattr(exc, "stderr_tail", ""),
                        "app_server_returncode": getattr(exc, "returncode", None),
                        "pending_run_id": None,
                        "active_harness_cycle_id": cycle_id,
                    })
                    self._save(state)
                    return state
                finally:
                    self._close_harness_cycle(cycle_id)
                self._reconcile_completed_run_count(state)
                new_runs = sorted(run_ids)
                if not new_runs:
                    if self._goal_error:
                        repaired_goal = self._goal_spec()
                        if repaired_goal is not None and self._goal_error is None:
                            client.set_goal(thread_id, self._goal_objective(repaired_goal))
                            state.pop("goal_contract_error", None)
                            state.update({"phase": "GOAL_READY", "pending_run_id": None})
                            current_prompt = self._experiment_prompt()
                            self._save(state)
                            continue
                        state.update({
                            "phase": "GOAL_REPAIR",
                            "goal_contract_error": self._goal_error,
                            "pending_run_id": None,
                        })
                        current_prompt = self._goal_repair_prompt(objective, prompt)
                        self._save(state)
                        continue
                    if self._goal_contract_path().exists() and not state.get("completed_runs", 0):
                        # The first turn is intentionally a no-experiment goal
                        # refinement turn. Update the App Server Goal to the
                        # refined contract before asking for the first idea.
                        refined_goal = self._goal_spec()
                        if refined_goal:
                            client.set_goal(thread_id, self._goal_objective(refined_goal))
                        state.update({"phase": "GOAL_READY", "pending_run_id": None})
                        current_prompt = self._experiment_prompt()
                        self._save(state)
                        continue
                    decision = self._read_goal_decision()
                    if self._decision_error:
                        state.update({
                            "phase": "GOAL_DECISION_REPAIR",
                            "goal_decision_error": self._decision_error,
                            "pending_run_id": None,
                        })
                        current_prompt = self._goal_decision_repair_prompt(objective, prompt)
                        self._save(state)
                        continue
                    if decision and decision.get("status") == "complete":
                        state.update({
                            "phase": "COMPLETED",
                            "stop_reason": decision.get("decision"),
                            "reason": decision.get("reason", "Codex Goal declared the research complete"),
                            "pending_run_id": None,
                        })
                        self._save(state)
                        return state
                    # A Goal turn can end without starting an experiment and
                    # without writing a terminal decision. That is not a
                    # research conclusion while budget remains; give the same
                    # thread another bounded turn instead of silently stopping.
                    state.update({
                        "phase": "GOAL_RETRY",
                        "reason": "Goal ended without a new run or an explicit goal_decision.json; retrying within cycle budget",
                        "pending_run_id": None,
                    })
                    current_prompt = (
                        "上一个 turn 没有启动实验，也没有写入 research/goal_decision.json。"
                        "目标尚未达到且实验预算仍可能存在，请重新读取历史结果和 goal_contract，"
                        "要么只启动一个新的、未验证的实验，要么明确写入 goal_decision.json；不要无声结束 turn。\n"
                        + current_prompt
                    )
                    self._save(state)
                    continue
                if len(new_runs) > 1:
                    # The current Harness is intentionally serial. Preserve
                    # the durable run ids and pause for reconciliation instead
                    # of terminating because of an unexpected model/tool
                    # sequence or a misconfigured parallel mode.
                    state.update({
                        "phase": "PAUSED",
                        "reason": "multiple experiments were submitted in one turn; reconcile active runs before resuming",
                        "unexpected_run_ids": new_runs,
                        "pending_run_id": None,
                    })
                    self._save(state)
                    return state
                pending_run_id = new_runs[0]
                self.runner.annotate_run(
                    pending_run_id,
                    codex_model=client.model,
                    codex_reasoning_effort=client.reasoning_effort,
                )
                state.update({"phase": "WAITING_FOR_EVENT", "pending_run_id": pending_run_id})
                self._save(state)
                try:
                    self._sync_goal_if_contract_changed(client, thread_id, contract_digest)
                    state.update({"goal_sync_pending": False})
                except Exception:
                    # The worker is already durable. Persist the pending run
                    # before surfacing the sync failure so a later invocation
                    # can resume the run and retry Goal synchronization.
                    state.update({"goal_sync_pending": True})
                    raise
                self._save(state)

            # A cycle may have submitted an experiment just before the budget
            # was exhausted. Reconcile a terminal run now so state never says
            # "pending" for work that has already finished.
            if pending_run_id and self.runner.get_result(pending_run_id) is not None:
                self._consume_pending_result(state, pending_run_id)
                pending_run_id = None
            state.update({
                "phase": "PAUSED",
                "stop_reason": "cycle_limit",
                "reason": "max_cycles reached",
                "pending_run_id": pending_run_id,
            })
            self._save(state)
            return state
        except (AppServerTimeoutError, AppServerConnectionError, RuntimeError, OSError, KeyError, TypeError) as exc:
            if startup_complete:
                raise
            # Persist startup failures, including silent response timeouts.
            # This prevents an apparently dead process from leaving the lock
            # held while losing the thread id and diagnostic context.
            thread_id = client.last_thread_id if client is not None else state.get("thread_id")
            state.update({
                "phase": "APP_SERVER_STALLED" if isinstance(exc, AppServerTimeoutError) else "APP_SERVER_STARTUP_FAILED",
                "reason": str(exc),
                "thread_id": thread_id,
                "app_server_context": getattr(exc, "context", "startup"),
                "app_server_stderr": getattr(client, "_stderr_tail", lambda: "")(),
                "app_server_returncode": client.process.poll() if client is not None else None,
            })
            self._save(state)
            return state
        except Exception as exc:
            # Never leave a misleading GOAL_RUNNING state after an unexpected
            # controller bug. Preserve the exception class and a concise
            # traceback so the next invocation can diagnose and resume.
            import traceback
            state.update({
                "phase": "HARNESS_FAILED",
                "reason": f"{type(exc).__name__}: {exc}",
                "error_traceback": traceback.format_exc()[-8000:],
                "pending_run_id": state.get("pending_run_id"),
            })
            self._save(state)
            raise
        finally:
            if client is not None:
                client.close()
            lock.__exit__(None, None, None)

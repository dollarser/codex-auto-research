"""Minimal Codex App Server client used only to wake one persisted Goal."""

from __future__ import annotations

import io
import json
import selectors
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Self

from .config import ResearchConfig, load_config


class AppServerError(RuntimeError):
    pass


class AppServerTimeout(AppServerError):
    pass


def _turn_id(message: dict[str, Any]) -> str | None:
    params = message.get("params", {})
    if not isinstance(params, dict):
        return None
    direct = params.get("turnId")
    if isinstance(direct, str):
        return direct
    turn = params.get("turn")
    return (
        turn.get("id")
        if isinstance(turn, dict) and isinstance(turn.get("id"), str)
        else None
    )


class AppServerClient:
    """Dependency-free JSONL client for the small wake-up protocol surface."""

    QUIESCENT_GOAL_STATUSES = frozenset(
        {"paused", "blocked", "usageLimited", "budgetLimited", "complete"}
    )

    def __init__(self, cwd: str | Path, config: ResearchConfig | None = None):
        self.cwd = str(Path(cwd).resolve())
        self.config = config or load_config(self.cwd)
        self._next_id = 1
        self._pending: deque[dict[str, Any]] = deque()
        self._stderr: deque[str] = deque(maxlen=100)
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

    def _drain_stderr(self) -> None:
        if self.process.stderr:
            for line in self.process.stderr:
                self._stderr.append(line.rstrip())

    def _stderr_tail(self) -> str:
        return "\n".join(self._stderr)[-4000:]

    def _readline(self, timeout_s: float, context: str) -> str:
        if not self.process.stdout:
            raise AppServerError("App Server stdout is closed")
        stream = self.process.stdout
        try:
            fd = stream.fileno()
        except (AttributeError, io.UnsupportedOperation, OSError, ValueError):
            line = stream.readline()
            if not line:
                raise AppServerError(f"App Server closed during {context}")
            return line
        selector = selectors.DefaultSelector()
        try:
            selector.register(fd, selectors.EVENT_READ)
            if not selector.select(timeout_s):
                detail = self._stderr_tail()
                raise AppServerTimeout(
                    f"App Server produced no data for {context} within {timeout_s:.1f}s"
                    + (f"; stderr={detail[-1000:]}" if detail else "")
                )
            line = stream.readline()
        finally:
            selector.close()
        if not line:
            raise AppServerError(
                f"App Server stdout closed during {context}; stderr={self._stderr_tail()}"
            )
        return line

    def _write(self, message: dict[str, Any]) -> None:
        if not self.process.stdin:
            raise AppServerError("App Server stdin is closed")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _send(self, method: str, params: dict[str, Any]) -> int:
        request_id = self._next_id
        self._next_id += 1
        self._write({"id": request_id, "method": method, "params": params})
        return request_id

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"method": method, "params": params})

    def _handle_server_request(self, message: dict[str, Any]) -> bool:
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
            result = {"decision": "decline"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "cancel"}
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        else:
            self._write(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"unsupported request: {method}",
                    },
                }
            )
            return True
        self._write({"id": request_id, "result": result})
        return True

    def _message(self, timeout_s: float, context: str) -> dict[str, Any]:
        if self._pending:
            return self._pending.popleft()
        line = self._readline(timeout_s, context)
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AppServerError(f"invalid App Server JSONL: {line[:500]!r}") from exc
        if not isinstance(value, dict):
            raise AppServerError("App Server message must be an object")
        return value

    def _response(
        self, request_id: int, timeout_s: float | None = None
    ) -> dict[str, Any]:
        timeout_s = timeout_s or self.config.app_server_response_timeout_s
        deferred: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTimeout(f"App Server response {request_id} timed out")
            message = self._message(remaining, f"response {request_id}")
            if self._handle_server_request(message):
                continue
            if message.get("id") == request_id:
                self._pending.extend(deferred)
                if "error" in message:
                    raise AppServerError(
                        json.dumps(message["error"], ensure_ascii=False)
                    )
                return message
            deferred.append(message)

    def initialize(self) -> None:
        request_id = self._send(
            "initialize",
            {
                "clientInfo": {
                    "name": "auto-research-goal-wake-listener",
                    "version": "0.3.0",
                }
            },
        )
        self._response(request_id)
        self._notify("initialized", {})

    def list_threads(self) -> list[dict[str, Any]]:
        threads: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "limit": 100,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "cwd": self.cwd,
            }
            if cursor:
                params["cursor"] = cursor
            response = self._response(self._send("thread/list", params))
            result = response.get("result", {})
            page = result.get("data", []) if isinstance(result, dict) else []
            threads.extend(item for item in page if isinstance(item, dict))
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not isinstance(cursor, str) or not cursor:
                return threads

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        response = self._response(self._send("thread/resume", {"threadId": thread_id}))
        thread = response.get("result", {}).get("thread", {})
        if not isinstance(thread, dict):
            raise AppServerError("thread/resume did not return a thread")
        return thread

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        response = self._response(
            self._send("thread/read", {"threadId": thread_id, "includeTurns": False})
        )
        thread = response.get("result", {}).get("thread", {})
        if not isinstance(thread, dict):
            raise AppServerError("thread/read did not return a thread")
        return thread

    def get_goal(self, thread_id: str) -> dict[str, Any] | None:
        response = self._response(
            self._send("thread/goal/get", {"threadId": thread_id})
        )
        goal = response.get("result", {}).get("goal")
        return goal if isinstance(goal, dict) else None

    def set_goal_status(self, thread_id: str, status: str) -> dict[str, Any]:
        response = self._response(
            self._send("thread/goal/set", {"threadId": thread_id, "status": status})
        )
        goal = response.get("result", {}).get("goal", {})
        if not isinstance(goal, dict) or goal.get("status") != status:
            raise AppServerError(f"Goal did not enter {status!r}")
        return goal

    def start_turn(self, thread_id: str, prompt: str) -> str:
        sandbox_type = (
            "dangerFullAccess"
            if self.config.codex_sandbox == "danger-full-access"
            else "workspaceWrite"
        )
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": self.cwd,
            "approvalPolicy": self.config.codex_approval,
            "sandboxPolicy": {"type": sandbox_type},
        }
        if self.config.codex_model:
            params["model"] = self.config.codex_model
        if self.config.codex_reasoning_effort:
            params["effort"] = self.config.codex_reasoning_effort
        response = self._response(self._send("turn/start", params))
        turn = response.get("result", {}).get("turn", {})
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise AppServerError("turn/start did not return a turn id")
        return turn_id

    def wait_until_goal_quiescent(self, thread_id: str, initial_turn_id: str) -> str:
        """Keep the execution host alive until Codex pauses or completes its Goal."""
        deadline = time.monotonic() + self.config.resumed_turn_timeout_s
        active_turns = {initial_turn_id}
        quiescent_status: str | None = None
        quiescent_since: float | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTimeout(
                    f"resumed Goal did not become quiescent within {self.config.resumed_turn_timeout_s:.1f}s"
                )
            read_timeout = min(remaining, 30.0) if quiescent_status else remaining
            try:
                message = self._message(
                    read_timeout, f"Goal {thread_id} to become quiescent"
                )
            except AppServerTimeout:
                if quiescent_status and quiescent_since is not None:
                    # Some App Server builds omit turn/completed after the Goal
                    # itself has durably paused. Give the turn a bounded flush
                    # window, then preserve the durable Goal state and exit.
                    return quiescent_status
                raise
            if self._handle_server_request(message):
                continue
            method = message.get("method")
            params = message.get("params", {})
            if method == "thread/goal/updated" and isinstance(params, dict):
                goal = params.get("goal", {})
                status = goal.get("status") if isinstance(goal, dict) else None
                if status in self.QUIESCENT_GOAL_STATUSES:
                    quiescent_status = str(status)
                    quiescent_since = quiescent_since or time.monotonic()
            if method == "turn/started":
                found = _turn_id(message)
                if found:
                    active_turns.add(found)
            if method == "turn/completed":
                active_turns.discard(_turn_id(message))
                goal = self.get_goal(thread_id)
                status = goal.get("status") if goal else None
                if status in self.QUIESCENT_GOAL_STATUSES:
                    quiescent_status = str(status)
                    quiescent_since = quiescent_since or time.monotonic()
                # An active Goal may schedule another native turn. Keep this
                # App Server process alive, but do not create or poll turns.
            if method == "item/completed" and isinstance(params, dict):
                item = params.get("item", {})
                if (
                    isinstance(item, dict)
                    and item.get("type") == "agentMessage"
                    and item.get("phase") == "final_answer"
                ):
                    active_turns.discard(_turn_id(message))
            if quiescent_status and not active_turns:
                return quiescent_status

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

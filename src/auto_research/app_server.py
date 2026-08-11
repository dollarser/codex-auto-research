"""Small JSONL client for Codex App Server threads, Goals, and Turns."""

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


class AppServerClient:
    """Dependency-free JSONL client for the auto-research control surface."""

    def __init__(
        self,
        cwd: str | Path,
        config: ResearchConfig | None = None,
        *,
        client_name: str = "auto-research-goal-wake-listener",
        client_version: str = "0.3.0",
    ):
        self.cwd = str(Path(cwd).resolve())
        self.config = config or load_config(self.cwd)
        self.client_name = client_name
        self.client_version = client_version
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
                    "name": self.client_name,
                    "version": self.client_version,
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

    def read_thread(
        self, thread_id: str, *, include_turns: bool = False
    ) -> dict[str, Any]:
        response = self._response(
            self._send(
                "thread/read",
                {"threadId": thread_id, "includeTurns": include_turns},
            )
        )
        thread = response.get("result", {}).get("thread", {})
        if not isinstance(thread, dict):
            raise AppServerError("thread/read did not return a thread")
        return thread

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        response = self._response(self._send("thread/resume", {"threadId": thread_id}))
        thread = response.get("result", {}).get("thread", {})
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise AppServerError("thread/resume did not return the requested thread")
        return thread

    def start_turn(
        self,
        thread_id: str,
        text: str,
        *,
        approval_policy: str | None = None,
        sandbox_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if approval_policy is not None:
            params["approvalPolicy"] = approval_policy
        if sandbox_policy is not None:
            params["sandboxPolicy"] = sandbox_policy
        response = self._response(self._send("turn/start", params))
        turn = response.get("result", {}).get("turn", {})
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise AppServerError("turn/start did not return a turn id")
        return turn

    @staticmethod
    def _notification_turn(message: dict[str, Any]) -> dict[str, Any] | None:
        params = message.get("params")
        if not isinstance(params, dict):
            return None
        turn = params.get("turn")
        if isinstance(turn, dict):
            return turn
        return params if isinstance(params.get("id"), str) else None

    def wait_turn(
        self, thread_id: str, turn_id: str, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        """Consume App Server events until the exact Turn reaches completion."""
        timeout_s = timeout_s or 24 * 3600
        deadline = time.monotonic() + timeout_s
        deferred: list[dict[str, Any]] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerTimeout(f"Turn {turn_id} timed out")
                message = self._message(remaining, f"turn {turn_id}")
                if self._handle_server_request(message):
                    continue
                method = message.get("method")
                turn = self._notification_turn(message)
                if method == "turn/completed" and turn is not None:
                    event_thread_id = message.get("params", {}).get("threadId")
                    if turn.get("id") == turn_id and event_thread_id in {
                        None,
                        thread_id,
                    }:
                        return turn
                deferred.append(message)
        finally:
            self._pending.extend(deferred)

    def start_thread(self, *, service_name: str) -> dict[str, Any]:
        response = self._response(
            self._send(
                "thread/start",
                {"cwd": self.cwd, "serviceName": service_name},
            )
        )
        thread = response.get("result", {}).get("thread", {})
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise AppServerError("thread/start did not return a thread id")
        return thread

    def set_thread_name(self, thread_id: str, name: str) -> None:
        self._response(
            self._send("thread/name/set", {"threadId": thread_id, "name": name})
        )

    def get_goal(self, thread_id: str) -> dict[str, Any] | None:
        response = self._response(
            self._send("thread/goal/get", {"threadId": thread_id})
        )
        goal = response.get("result", {}).get("goal")
        return goal if isinstance(goal, dict) else None

    def set_goal(
        self,
        thread_id: str,
        *,
        objective: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id}
        if objective is not None:
            params["objective"] = objective
        if status is not None:
            params["status"] = status
        response = self._response(self._send("thread/goal/set", params))
        goal = response.get("result", {}).get("goal", {})
        if not isinstance(goal, dict):
            raise AppServerError("thread/goal/set did not return a Goal")
        if status is not None and goal.get("status") != status:
            raise AppServerError(f"Goal did not enter {status!r}")
        if objective is not None and goal.get("objective") != objective:
            raise AppServerError("Goal objective was not persisted")
        return goal

    def set_goal_status(self, thread_id: str, status: str) -> dict[str, Any]:
        return self.set_goal(thread_id, status=status)

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

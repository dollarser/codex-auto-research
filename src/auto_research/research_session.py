"""Provision and persist one dedicated Codex research thread per project."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .app_server import AppServerClient
from .config import load_config
from .ledger import write_json_atomic
from .state_paths import resolve_state_root

SESSION_SCHEMA_VERSION = 1
SESSION_FILE_NAME = "codex_session.json"


class ResearchSessionError(RuntimeError):
    pass


def _validated_objective(value: str | None) -> str | None:
    if value is None:
        return None
    objective = value.strip()
    if not objective:
        raise ResearchSessionError("objective must not be empty")
    if len(objective) > 4000:
        raise ResearchSessionError("objective must be at most 4000 characters")
    return objective


def _read_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchSessionError(
            f"invalid {path}; refusing to create a duplicate thread"
        ) from exc
    if not isinstance(value, dict):
        raise ResearchSessionError(f"{path} must contain a JSON object")
    if value.get("schema_version") != SESSION_SCHEMA_VERSION:
        raise ResearchSessionError(
            f"unsupported research session schema in {path}"
        )
    thread_id = value.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ResearchSessionError(f"{path} does not contain a valid thread_id")
    return value


def read_bound_thread_id(project_dir: str | Path, state_root: str | Path | None = None) -> str | None:
    """Return the project's persisted dedicated thread, failing closed on damage."""
    project = Path(project_dir).resolve()
    state = _read_state(resolve_state_root(project, state_root) / SESSION_FILE_NAME)
    if state is None:
        return None
    recorded_project = state.get("project_root")
    if recorded_project != str(project):
        raise ResearchSessionError(
            "research session project_root does not match the current project"
        )
    if state.get("setup_state") != "ready":
        raise ResearchSessionError(
            "research session setup is incomplete; rerun auto-research session"
        )
    return str(state["thread_id"])


class ResearchSessionManager:
    """Create once on request, then validate and reuse the same Codex thread."""

    def __init__(
        self,
        project_dir: str | Path,
        *,
        state_file_name: str = SESSION_FILE_NAME,
        state_root: str | Path | None = None,
        client_factory: Callable[[], Any] | None = None,
    ):
        if Path(state_file_name).name != state_file_name:
            raise ResearchSessionError("state_file_name must be a plain file name")
        self.project_dir = Path(project_dir).resolve()
        self.config = load_config(self.project_dir)
        self.research_dir = resolve_state_root(self.project_dir, state_root)
        self.state_path = self.research_dir / state_file_name
        lock_name = (
            ".codex-session.lock"
            if state_file_name == SESSION_FILE_NAME
            else f".{Path(state_file_name).stem.replace('_', '-')}.lock"
        )
        self.lock_path = self.research_dir / lock_name
        self.client_factory = client_factory or (
            lambda: AppServerClient(
                self.project_dir,
                client_name="auto-research-session-bootstrap",
                client_version="0.5.0",
                managed_daemon=True,
            )
        )

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.research_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        import fcntl

        with self.lock_path.open("r+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _default_objective(self) -> str:
        goal_path = self.project_dir / "goal.json"
        try:
            goal = json.loads(goal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResearchSessionError(
                "an objective or a valid goal.json statement is required"
            ) from exc
        statement = goal.get("statement") if isinstance(goal, dict) else None
        objective = _validated_objective(
            statement if isinstance(statement, str) else None
        )
        if objective is None:
            raise ResearchSessionError("goal.json must contain a non-empty statement")
        return objective

    def _validate_project_binding(self, thread: dict[str, Any], thread_id: str) -> None:
        cwd = thread.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise ResearchSessionError(
                f"thread {thread_id} does not expose a project cwd"
            )
        if Path(cwd).resolve() != self.project_dir:
            raise ResearchSessionError(
                f"thread {thread_id} is bound to {cwd}, not {self.project_dir}"
            )

    def prepare(
        self,
        *,
        create_thread: bool = False,
        thread_id: str | None = None,
        objective: str | None = None,
        title: str | None = None,
        replace_goal: bool = False,
    ) -> dict[str, Any]:
        """Provision or reuse the project's dedicated research thread.

        ``create_thread`` is intentionally idempotent: it only creates when no
        project binding exists. A stored binding always wins, so retries cannot
        fan out into duplicate Codex threads.
        """
        objective = _validated_objective(objective)
        title = title.strip() if title is not None else None
        if title == "":
            raise ResearchSessionError("title must not be empty")
        if create_thread and thread_id:
            raise ResearchSessionError(
                "--create-thread and --thread-id are mutually exclusive"
            )
        if replace_goal and objective is None:
            raise ResearchSessionError("--replace-goal requires --objective")

        with self._lock():
            state = _read_state(self.state_path)
            if state is not None:
                if state.get("project_root") != str(self.project_dir):
                    raise ResearchSessionError(
                        "stored research session belongs to another project"
                    )
                bound_thread_id = str(state["thread_id"])
                if thread_id and thread_id != bound_thread_id:
                    raise ResearchSessionError(
                        f"project is already bound to thread {bound_thread_id}; "
                        "refusing to create or adopt another thread"
                    )
                thread_id = bound_thread_id
            elif not thread_id and not create_thread:
                raise ResearchSessionError(
                    "no dedicated research thread is bound; pass --create-thread "
                    "once or bind an existing task with --thread-id"
                )

            creation_objective = objective
            if state is None and create_thread and creation_objective is None:
                # Validate all local inputs before the irreversible thread/start
                # request, so an invalid goal file cannot leave an untracked task.
                creation_objective = self._default_objective()

            now = time.time()
            created = False
            with self.client_factory() as client:
                client.initialize()
                if state is None and create_thread:
                    started_thread = client.start_thread(
                        service_name="auto-research-session-bootstrap",
                        model=self.config.codex_model,
                        approval_policy=self.config.codex_approval_policy,
                        sandbox=self.config.codex_sandbox,
                    )
                    thread_id = str(started_thread["id"])
                    created = True
                    chosen_title = title or f"Auto Research · {self.project_dir.name}"
                    assert creation_objective is not None
                    chosen_objective = creation_objective
                    state = {
                        "schema_version": SESSION_SCHEMA_VERSION,
                        "project_root": str(self.project_dir),
                        "thread_id": thread_id,
                        "title": chosen_title,
                        "objective": chosen_objective,
                        "ownership": "auto_created",
                        "model": self.config.codex_model,
                        "approval_policy": self.config.codex_approval_policy,
                        "sandbox": self.config.codex_sandbox,
                        "setup_state": "initializing",
                        "created_at": now,
                        "updated_at": now,
                    }
                    # Persist the returned id before any follow-up RPC. If naming
                    # or Goal setup fails, a retry resumes this same thread.
                    write_json_atomic(self.state_path, state)
                    thread = client.read_thread(thread_id)
                else:
                    assert thread_id is not None
                    thread = client.read_thread(thread_id)

                assert state is not None or thread_id is not None
                self._validate_project_binding(thread, str(thread_id))

                if state is None:
                    current_goal = client.get_goal(str(thread_id))
                    chosen_objective = (
                        objective
                        or (
                            current_goal.get("objective")
                            if isinstance(current_goal, dict)
                            and isinstance(current_goal.get("objective"), str)
                            else None
                        )
                        or self._default_objective()
                    )
                    chosen_title = title or f"Auto Research · {self.project_dir.name}"
                    state = {
                        "schema_version": SESSION_SCHEMA_VERSION,
                        "project_root": str(self.project_dir),
                        "thread_id": str(thread_id),
                        "title": chosen_title,
                        "objective": chosen_objective,
                        "ownership": "adopted",
                        "setup_state": "initializing",
                        "created_at": now,
                        "updated_at": now,
                    }
                    write_json_atomic(self.state_path, state)

                chosen_title = title or str(state.get("title") or "").strip()
                if not chosen_title:
                    chosen_title = f"Auto Research · {self.project_dir.name}"
                if state.get("setup_state") != "ready" or title is not None:
                    client.set_thread_name(str(thread_id), chosen_title)

                current_goal = client.get_goal(str(thread_id))
                current_objective = (
                    current_goal.get("objective")
                    if isinstance(current_goal, dict)
                    and isinstance(current_goal.get("objective"), str)
                    else None
                )
                current_goal_complete = bool(
                    isinstance(current_goal, dict)
                    and current_goal.get("status") == "complete"
                )
                requested_objective = objective
                if (
                    requested_objective is not None
                    and current_objective is not None
                    and requested_objective != current_objective
                    and not current_goal_complete
                    and not replace_goal
                ):
                    raise ResearchSessionError(
                        "the dedicated thread already has a different Goal; "
                        "pass --replace-goal only after confirming the replacement"
                    )
                if current_goal is None or current_goal_complete:
                    goal_objective = (
                        requested_objective
                        or str(state.get("objective") or "").strip()
                        or self._default_objective()
                    )
                    current_goal = client.set_goal(
                        str(thread_id), objective=goal_objective, status="paused"
                    )
                elif requested_objective is not None and (
                    replace_goal or requested_objective == current_objective
                ):
                    if requested_objective != current_objective:
                        current_goal = client.set_goal(
                            str(thread_id),
                            objective=requested_objective,
                            status="paused",
                        )

                persisted_objective = (
                    current_goal.get("objective")
                    if isinstance(current_goal, dict)
                    and isinstance(current_goal.get("objective"), str)
                    else str(state.get("objective") or "")
                )
                state.update(
                    {
                        "title": chosen_title,
                        "objective": persisted_objective,
                        "model": self.config.codex_model,
                        "approval_policy": self.config.codex_approval_policy,
                        "sandbox": self.config.codex_sandbox,
                        "setup_state": "ready",
                        "updated_at": time.time(),
                    }
                )
                write_json_atomic(self.state_path, state)
                return {
                    "thread_id": str(thread_id),
                    "project_root": str(self.project_dir),
                    "title": chosen_title,
                    "objective": persisted_objective,
                    "goal_status": current_goal.get("status")
                    if isinstance(current_goal, dict)
                    else None,
                    "created": created,
                    "model": self.config.codex_model,
                    "approval_policy": self.config.codex_approval_policy,
                    "sandbox": self.config.codex_sandbox,
                    "reused": not created,
                    "state_file": str(self.state_path),
                }

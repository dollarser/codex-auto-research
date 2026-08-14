"""Provision one Codex research session in its Thread-keyed state root."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .app_server import AppServerClient
from .config import load_config
from .ledger import read_json, write_json_atomic
from .state_paths import supervisors_root, thread_state_root, validate_thread_id

SESSION_SCHEMA_VERSION = 2
SESSION_FILE_NAME = "supervisor_session.json"
METADATA_SCHEMA_VERSION = 1
CYCLE_SCHEMA_VERSION = 1


class ResearchSessionError(RuntimeError):
    pass


def _validated_objective(value: str | None) -> str | None:
    if value is None:
        return None
    objective = value.strip()
    if not objective:
        raise ResearchSessionError("objective must not be empty")
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
        raise ResearchSessionError(f"unsupported research session schema in {path}")
    thread_id = value.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ResearchSessionError(f"{path} does not contain a valid thread_id")
    return value


def read_bound_thread_id(
    project_dir: str | Path,
    thread_id: str | None = None,
) -> str | None:
    """Validate and return one exact Thread binding; never search other roots."""
    if thread_id is None:
        return None
    selected = validate_thread_id(thread_id)
    project = Path(project_dir).resolve()
    state = _read_state(thread_state_root(project, selected) / SESSION_FILE_NAME)
    if state is None:
        return None
    if state.get("project_root") != str(project):
        raise ResearchSessionError(
            "research session project_root does not match the current project"
        )
    if state.get("thread_id") != selected:
        raise ResearchSessionError("research session directory and thread_id disagree")
    if state.get("setup_state") != "ready":
        raise ResearchSessionError(
            "research session setup is incomplete; rerun auto-research session"
        )
    return selected


class ResearchSessionManager:
    """Create or adopt one Thread and persist only below its canonical root."""

    def __init__(
        self,
        project_dir: str | Path,
        *,
        thread_id: str | None = None,
        client_factory: Callable[[], Any] | None = None,
    ):
        self.project_dir = Path(project_dir).resolve()
        self.config = load_config(self.project_dir)
        self.thread_id = validate_thread_id(thread_id) if thread_id else None
        self.client_factory = client_factory or (
            lambda: AppServerClient(
                self.project_dir,
                client_name="auto-research-session-bootstrap",
                client_version="0.7.0",
                managed_daemon=True,
            )
        )

    @property
    def research_dir(self) -> Path:
        if self.thread_id is None:
            raise ResearchSessionError("session has no Thread id yet")
        return thread_state_root(self.project_dir, self.thread_id)

    @property
    def state_path(self) -> Path:
        return self.research_dir / SESSION_FILE_NAME

    @contextmanager
    def _file_lock(self, path: Path) -> Iterator[None]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        import fcntl

        with path.open("r+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _default_objective(self) -> str:
        goal_path = self.project_dir / "GOAL.md"
        try:
            objective = _validated_objective(goal_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ResearchSessionError(
                "an objective or a non-empty GOAL.md is required"
            ) from exc
        if objective is None:
            raise ResearchSessionError("GOAL.md must contain a non-empty objective")
        return objective

    def _validate_project_binding(self, thread: dict[str, Any], thread_id: str) -> None:
        cwd = thread.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise ResearchSessionError(f"thread {thread_id} does not expose a project cwd")
        if Path(cwd).resolve() != self.project_dir:
            raise ResearchSessionError(
                f"thread {thread_id} is bound to {cwd}, not {self.project_dir}"
            )

    def _write_metadata(self, title: str, now: float) -> None:
        path = self.research_dir / "metadata.json"
        previous = read_json(path, {}) or {}
        if previous and (
            previous.get("project_root") != str(self.project_dir)
            or previous.get("thread_id") != self.thread_id
        ):
            raise ResearchSessionError("metadata.json does not match its Thread root")
        write_json_atomic(
            path,
            {
                "schema_version": METADATA_SCHEMA_VERSION,
                "project_root": str(self.project_dir),
                "thread_id": self.thread_id,
                "name": title,
                "created_at": previous.get("created_at", now),
                "updated_at": now,
            },
        )

    def _record_cycle(self, objective: str, now: float) -> str:
        cycle_id = f"cycle-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        write_json_atomic(
            self.research_dir / "cycles" / f"{cycle_id}.json",
            {
                "schema_version": CYCLE_SCHEMA_VERSION,
                "cycle_id": cycle_id,
                "thread_id": self.thread_id,
                "objective": objective,
                "started_at": now,
            },
        )
        return cycle_id

    def _initial_state(
        self,
        *,
        title: str,
        objective: str,
        ownership: str,
        now: float,
    ) -> dict[str, Any]:
        assert self.thread_id is not None
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "project_root": str(self.project_dir),
            "thread_id": self.thread_id,
            "title": title,
            "objective_digest": hashlib.sha256(objective.encode("utf-8")).hexdigest(),
            "ownership": ownership,
            "setup_state": "initializing",
            "created_at": now,
            "updated_at": now,
        }

    def validate_existing(self) -> dict[str, Any]:
        """Validate one ready binding without creating or replacing a Goal."""
        if self.thread_id is None:
            raise ResearchSessionError("validation requires an explicit Thread id")
        state = _read_state(self.state_path)
        if state is None:
            raise ResearchSessionError(
                "research session binding is missing; run auto-research session explicitly"
            )
        if state.get("project_root") != str(self.project_dir):
            raise ResearchSessionError("stored session belongs to another project")
        if state.get("thread_id") != self.thread_id:
            raise ResearchSessionError("session binding disagrees with its directory")
        if state.get("setup_state") != "ready":
            raise ResearchSessionError("research session setup is incomplete")
        with self.client_factory() as client:
            client.initialize()
            thread = client.read_thread(self.thread_id)
            self._validate_project_binding(thread, self.thread_id)
            goal = client.get_goal(self.thread_id)
        return {
            "thread_id": self.thread_id,
            "state_root": str(self.research_dir),
            "project_root": str(self.project_dir),
            "title": state.get("title"),
            "objective": (
                goal.get("objective") if isinstance(goal, dict) else None
            ),
            "goal_status": goal.get("status") if isinstance(goal, dict) else None,
            "cycle_id": state.get("current_cycle_id"),
            "state_file": str(self.state_path),
        }

    def restart_goal(self, *, objective: str, title: str | None = None) -> dict[str, Any]:
        """Explicitly start a new Goal cycle on an already-bound Thread."""
        validated = _validated_objective(objective)
        assert validated is not None
        if self.thread_id is None:
            raise ResearchSessionError("restart requires an explicit Thread id")
        lock_name = ".supervisor-session.lock"
        with self._file_lock(self.research_dir / lock_name):
            state = _read_state(self.state_path)
            if state is None or state.get("setup_state") != "ready":
                raise ResearchSessionError("restart requires a ready research session")
            with self.client_factory() as client:
                client.initialize()
                thread = client.read_thread(self.thread_id)
                self._validate_project_binding(thread, self.thread_id)
                goal = client.get_goal(self.thread_id)
                if isinstance(goal, dict) and goal.get("status") != "complete":
                    raise ResearchSessionError(
                        "restart requires the existing native Goal to be complete or absent"
                    )
                current_title = (title or str(state.get("title") or "")).strip()
                if not current_title:
                    current_title = f"Auto Research · {self.project_dir.name}"
                if title is not None:
                    client.set_thread_name(self.thread_id, current_title)
                goal = client.set_goal(
                    self.thread_id, objective=validated, status="paused"
                )
            now = time.time()
            cycle_id = self._record_cycle(validated, now)
            state.update(
                {
                    "title": current_title,
                    "objective_digest": hashlib.sha256(
                        validated.encode("utf-8")
                    ).hexdigest(),
                    "current_cycle_id": cycle_id,
                    "updated_at": now,
                }
            )
            state.pop("objective", None)
            write_json_atomic(self.state_path, state)
            self._write_metadata(current_title, now)
            return {
                "thread_id": self.thread_id,
                "state_root": str(self.research_dir),
                "project_root": str(self.project_dir),
                "title": current_title,
                "objective": validated,
                "goal_status": goal.get("status"),
                "cycle_id": cycle_id,
                "state_file": str(self.state_path),
            }

    def prepare(
        self,
        *,
        create_thread: bool = False,
        thread_id: str | None = None,
        objective: str | None = None,
        title: str | None = None,
        replace_goal: bool = False,
        creation_key: str | None = None,
    ) -> dict[str, Any]:
        objective = _validated_objective(objective)
        title = title.strip() if title is not None else None
        if title == "":
            raise ResearchSessionError("title must not be empty")
        if create_thread and thread_id:
            raise ResearchSessionError("--create-thread and --thread-id are mutually exclusive")
        if create_thread and self.thread_id is None:
            creation_key = (creation_key or "").strip()
            if not creation_key:
                raise ResearchSessionError("--create-thread requires --creation-key")
        if replace_goal and objective is None:
            raise ResearchSessionError("--replace-goal requires --objective")
        if thread_id:
            selected = validate_thread_id(thread_id)
            if self.thread_id and self.thread_id != selected:
                raise ResearchSessionError("session manager is bound to another Thread")
            self.thread_id = selected
        if create_thread and self.thread_id is None and objective is None:
            objective = self._default_objective()
        if not create_thread and self.thread_id is None:
            raise ResearchSessionError(
                "no research Thread selected; pass --create-thread or --thread-id"
            )

        bootstrap_lock = supervisors_root(self.project_dir) / ".thread-bootstrap.lock"
        with self._file_lock(bootstrap_lock):  # noqa: SIM117 - lock must precede client
            with self.client_factory() as client:
                client.initialize()
                created = False
                if create_thread and self.thread_id is None:
                    assert creation_key is not None
                    creation_id = hashlib.sha256(
                        creation_key.encode("utf-8")
                    ).hexdigest()
                    creation_path = (
                        supervisors_root(self.project_dir)
                        / "thread_creations"
                        / f"{creation_id}.json"
                    )
                    creation = read_json(creation_path, None)
                    if isinstance(creation, dict) and creation.get("status") == "READY":
                        self.thread_id = validate_thread_id(str(creation["thread_id"]))
                    elif creation_path.exists():
                        raise ResearchSessionError(
                            "a prior Thread creation with this key did not finish; "
                            f"inspect {creation_path} instead of creating a duplicate"
                        )
                    else:
                        write_json_atomic(
                            creation_path,
                            {
                                "creation_id": creation_id,
                                "project_root": str(self.project_dir),
                                "status": "CREATING",
                                "started_at": time.time(),
                            },
                        )
                        started = client.start_thread(
                            service_name="auto-research-session-bootstrap",
                            model=self.config.codex_model,
                            approval_policy=self.config.codex_approval_policy,
                            sandbox=self.config.codex_sandbox,
                        )
                        self.thread_id = validate_thread_id(str(started["id"]))
                        write_json_atomic(
                            creation_path,
                            {
                                "creation_id": creation_id,
                                "project_root": str(self.project_dir),
                                "status": "READY",
                                "thread_id": self.thread_id,
                                "finished_at": time.time(),
                            },
                        )
                        created = True
                assert self.thread_id is not None

                lock_name = ".supervisor-session.lock"
                with self._file_lock(self.research_dir / lock_name):
                    state = _read_state(self.state_path)
                    binding_preexisted = bool(
                        state is not None and state.get("setup_state") == "ready"
                    )
                    now = time.time()
                    chosen_title = title or f"Auto Research · {self.project_dir.name}"
                    if state is None and created:
                        assert objective is not None
                        state = self._initial_state(
                            title=chosen_title,
                            objective=objective,
                            ownership="auto_created",
                            now=now,
                        )
                        # The new Thread becomes tracked before any follow-up RPC.
                        write_json_atomic(self.state_path, state)
                        self._write_metadata(chosen_title, now)

                    thread = client.read_thread(self.thread_id)
                    self._validate_project_binding(thread, self.thread_id)
                    current_goal = client.get_goal(self.thread_id)

                    if state is None:
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
                        state = self._initial_state(
                            title=chosen_title,
                            objective=chosen_objective,
                            ownership="adopted",
                            now=now,
                        )
                        write_json_atomic(self.state_path, state)
                        self._write_metadata(chosen_title, now)
                    else:
                        if state.get("project_root") != str(self.project_dir):
                            raise ResearchSessionError("stored session belongs to another project")
                        if state.get("thread_id") != self.thread_id:
                            raise ResearchSessionError("session binding disagrees with its directory")
                        chosen_title = title or str(state.get("title") or chosen_title)

                    if state.get("setup_state") != "ready" or title is not None:
                        client.set_thread_name(self.thread_id, chosen_title)

                    current_objective = (
                        current_goal.get("objective")
                        if isinstance(current_goal, dict)
                        and isinstance(current_goal.get("objective"), str)
                        else None
                    )
                    current_complete = bool(
                        isinstance(current_goal, dict)
                        and current_goal.get("status") == "complete"
                    )
                    if (
                        objective is not None
                        and current_objective is not None
                        and objective != current_objective
                        and not current_complete
                        and not replace_goal
                    ):
                        raise ResearchSessionError(
                            "the Thread already has a different Goal; pass --replace-goal"
                        )

                    new_cycle = False
                    if current_goal is None or current_complete:
                        if binding_preexisted and not created:
                            raise ResearchSessionError(
                                "the bound Goal is complete or absent; use supervisor restart"
                            )
                        goal_objective = (
                            objective or self._default_objective()
                        )
                        current_goal = client.set_goal(
                            self.thread_id, objective=goal_objective, status="paused"
                        )
                        new_cycle = True
                    elif objective is not None and objective != current_objective:
                        current_goal = client.set_goal(
                            self.thread_id, objective=objective, status="paused"
                        )
                        new_cycle = True

                    persisted_objective = (
                        current_goal.get("objective")
                        if isinstance(current_goal, dict)
                        and isinstance(current_goal.get("objective"), str)
                        else str(state.get("objective") or "")
                    )
                    if new_cycle or not state.get("current_cycle_id"):
                        state["current_cycle_id"] = self._record_cycle(
                            persisted_objective, now
                        )
                    state.update(
                        {
                            "title": chosen_title,
                            "objective_digest": hashlib.sha256(
                                persisted_objective.encode("utf-8")
                            ).hexdigest(),
                            "model": self.config.codex_model,
                            "approval_policy": self.config.codex_approval_policy,
                            "sandbox": self.config.codex_sandbox,
                            "setup_state": "ready",
                            "updated_at": time.time(),
                        }
                    )
                    state.pop("objective", None)
                    write_json_atomic(self.state_path, state)
                    self._write_metadata(chosen_title, time.time())
                    return {
                        "thread_id": self.thread_id,
                        "state_root": str(self.research_dir),
                        "project_root": str(self.project_dir),
                        "title": chosen_title,
                        "objective": persisted_objective,
                        "goal_status": current_goal.get("status")
                        if isinstance(current_goal, dict)
                        else None,
                        "cycle_id": state["current_cycle_id"],
                        "created": created,
                        "reused": not created,
                        "state_file": str(self.state_path),
                    }

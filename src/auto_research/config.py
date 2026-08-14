"""Configuration for the native Goal Supervisor runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import tomllib


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value is not None and value.strip() else None


def _int(value: object, name: str, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return parsed


def _float(value: object, name: str, minimum: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return parsed


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class ResearchConfig:
    codex_model: str = "gpt-5.6-terra"
    codex_approval_policy: str = "never"
    codex_sandbox: str = "workspace-write"
    app_server_response_timeout_s: float = 60.0
    goal_turn_timeout_s: float = 1800.0
    event_poll_s: float = 0.25
    event_grace_s: float = 30.0
    default_experiment_timeout_s: int = 3600
    worker_heartbeat_s: float = 5.0


def load_config(project_dir: str | Path) -> ResearchConfig:
    """Load research/config.toml and apply explicit AUTO_RESEARCH_* overrides."""
    path = Path(project_dir).resolve() / "research" / "config.toml"
    file_data: dict = {}
    if path.exists():
        with path.open("rb") as stream:
            file_data = tomllib.load(stream)
    supervisor = file_data.get("supervisor", {})
    experiment = file_data.get("experiment", {})
    codex = file_data.get("codex", {})

    def value(section: dict, key: str, env_name: str, default: object) -> object:
        override = _env(env_name)
        return override if override is not None else section.get(key, default)

    return ResearchConfig(
        codex_model=_string(
            value(codex, "model", "AUTO_RESEARCH_CODEX_MODEL", "gpt-5.6-terra"),
            "codex.model",
        ),
        codex_approval_policy=_string(
            value(
                codex,
                "approval_policy",
                "AUTO_RESEARCH_CODEX_APPROVAL_POLICY",
                "never",
            ),
            "codex.approval_policy",
        ),
        codex_sandbox=_string(
            value(
                codex,
                "sandbox",
                "AUTO_RESEARCH_CODEX_SANDBOX",
                "workspace-write",
            ),
            "codex.sandbox",
        ),
        app_server_response_timeout_s=_float(
            value(
                supervisor,
                "app_server_response_timeout_s",
                "AUTO_RESEARCH_APP_SERVER_RESPONSE_TIMEOUT_S",
                60.0,
            ),
            "supervisor.app_server_response_timeout_s",
            1.0,
        ),
        goal_turn_timeout_s=_float(
            value(
                supervisor,
                "goal_turn_timeout_s",
                "AUTO_RESEARCH_GOAL_TURN_TIMEOUT_S",
                1800.0,
            ),
            "supervisor.goal_turn_timeout_s",
            1.0,
        ),
        event_poll_s=_float(
            value(supervisor, "event_poll_s", "AUTO_RESEARCH_EVENT_POLL_S", 0.25),
            "supervisor.event_poll_s",
        ),
        event_grace_s=_float(
            value(supervisor, "event_grace_s", "AUTO_RESEARCH_EVENT_GRACE_S", 30.0),
            "supervisor.event_grace_s",
        ),
        default_experiment_timeout_s=_int(
            value(
                experiment,
                "default_timeout_s",
                "AUTO_RESEARCH_DEFAULT_EXPERIMENT_TIMEOUT_S",
                3600,
            ),
            "experiment.default_timeout_s",
            1,
        ),
        worker_heartbeat_s=_float(
            value(
                experiment,
                "worker_heartbeat_s",
                "AUTO_RESEARCH_WORKER_HEARTBEAT_S",
                5.0,
            ),
            "experiment.worker_heartbeat_s",
            0.1,
        ),
    )

"""Configuration for the Supervisor runtime and legacy Desktop listener."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import tomllib

DEFAULT_ALLOWED_EXECUTABLES = {"python", "python3"}


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value is not None and value.strip() else None


def _bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


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
    bind_recency_s: float = 600.0
    reconnect_initial_s: float = 2.0
    reconnect_max_s: float = 60.0
    event_poll_s: float = 0.25
    event_grace_s: float = 30.0
    # Legacy Desktop Goal listener. The managed App Server Supervisor is the
    # default runtime and does not use this compatibility path.
    auto_wake: bool = False
    use_shell: bool = True
    allowed_executables: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_ALLOWED_EXECUTABLES)
    )
    default_experiment_timeout_s: int = 3600
    worker_heartbeat_s: float = 5.0
    one_active_experiment: bool = True


def load_config(project_dir: str | Path) -> ResearchConfig:
    """Load research/config.toml and apply explicit AUTO_RESEARCH_* overrides."""
    path = Path(project_dir).resolve() / "research" / "config.toml"
    file_data: dict = {}
    if path.exists():
        with path.open("rb") as stream:
            file_data = tomllib.load(stream)
    listener = file_data.get("listener", {})
    experiment = file_data.get("experiment", {})
    codex = file_data.get("codex", {})

    def value(section: dict, key: str, env_name: str, default: object) -> object:
        override = _env(env_name)
        return override if override is not None else section.get(key, default)

    configured_allowlist = value(
        experiment,
        "allowed_executables",
        "AUTO_RESEARCH_ALLOWED_EXECUTABLES",
        None,
    )
    if configured_allowlist is None:
        allowlist = DEFAULT_ALLOWED_EXECUTABLES.copy()
    elif isinstance(configured_allowlist, str):
        allowlist = {
            item.strip() for item in configured_allowlist.split(",") if item.strip()
        }
    else:
        allowlist = {
            str(item).strip() for item in configured_allowlist if str(item).strip()
        }
    if not allowlist:
        raise ValueError("experiment.allowed_executables must not be empty")

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
                listener,
                "app_server_response_timeout_s",
                "AUTO_RESEARCH_APP_SERVER_RESPONSE_TIMEOUT_S",
                60.0,
            ),
            "listener.app_server_response_timeout_s",
            1.0,
        ),
        bind_recency_s=_float(
            value(listener, "bind_recency_s", "AUTO_RESEARCH_BIND_RECENCY_S", 600.0),
            "listener.bind_recency_s",
            1.0,
        ),
        reconnect_initial_s=_float(
            value(
                listener,
                "reconnect_initial_s",
                "AUTO_RESEARCH_RECONNECT_INITIAL_S",
                2.0,
            ),
            "listener.reconnect_initial_s",
            0.1,
        ),
        reconnect_max_s=_float(
            value(listener, "reconnect_max_s", "AUTO_RESEARCH_RECONNECT_MAX_S", 60.0),
            "listener.reconnect_max_s",
            1.0,
        ),
        event_poll_s=_float(
            value(listener, "event_poll_s", "AUTO_RESEARCH_EVENT_POLL_S", 0.25),
            "listener.event_poll_s",
        ),
        event_grace_s=_float(
            value(listener, "event_grace_s", "AUTO_RESEARCH_EVENT_GRACE_S", 30.0),
            "listener.event_grace_s",
        ),
        auto_wake=_bool(
            value(listener, "auto_wake", "AUTO_RESEARCH_AUTO_WAKE", False),
            "listener.auto_wake",
        ),
        use_shell=_bool(
            value(experiment, "use_shell", "AUTO_RESEARCH_USE_SHELL", True),
            "experiment.use_shell",
        ),
        allowed_executables=frozenset(allowlist),
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
        one_active_experiment=_bool(
            value(
                experiment,
                "one_active_experiment",
                "AUTO_RESEARCH_ONE_ACTIVE_EXPERIMENT",
                True,
            ),
            "experiment.one_active_experiment",
        ),
    )

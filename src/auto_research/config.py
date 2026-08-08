"""Project-level Harness configuration with environment overrides."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CODEX_MODEL = "gpt-5.6-luna"
DEFAULT_CODEX_REASONING_EFFORT = "medium"
DEFAULT_ALLOWED_EXECUTABLES = {"python", "python3"}
DEFAULT_MAX_CYCLES = 1000
REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}


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


@dataclass(frozen=True)
class HarnessConfig:
    codex_model: str = DEFAULT_CODEX_MODEL
    codex_reasoning_effort: str = DEFAULT_CODEX_REASONING_EFFORT
    codex_sandbox: str = "danger-full-access"
    codex_approval: str = "never"
    max_cycles: int = DEFAULT_MAX_CYCLES
    reconnect_attempts: int = 3
    reconnect_backoff_s: float = 2.0
    app_server_response_timeout_s: float = 60.0
    app_server_turn_timeout_s: float = 900.0
    app_server_event_idle_timeout_s: float = 180.0
    event_poll_s: float = 0.25
    event_grace_s: float = 30.0
    use_shell: bool = True
    allowed_executables: frozenset[str] = field(default_factory=lambda: frozenset(DEFAULT_ALLOWED_EXECUTABLES))
    default_experiment_timeout_s: int = 3600
    worker_heartbeat_s: float = 5.0
    one_active_experiment: bool = True


def load_harness_config(project_dir: str | Path) -> HarnessConfig:
    """Load research/harness.toml, then apply AUTO_RESEARCH_* overrides."""
    path = Path(project_dir).resolve() / "research" / "harness.toml"
    file_data: dict = {}
    if path.exists():
        with path.open("rb") as stream:
            file_data = tomllib.load(stream)
    codex = file_data.get("codex", {})
    harness = file_data.get("harness", {})
    experiment = file_data.get("experiment", {})

    def value(section: dict, key: str, env_name: str, default: object) -> object:
        override = _env(env_name)
        return override if override is not None else section.get(key, default)

    model = str(value(codex, "model", "AUTO_RESEARCH_CODEX_MODEL", DEFAULT_CODEX_MODEL)).strip()
    effort = str(value(codex, "reasoning_effort", "AUTO_RESEARCH_CODEX_REASONING_EFFORT", DEFAULT_CODEX_REASONING_EFFORT)).strip()
    if effort not in REASONING_EFFORTS:
        raise ValueError(f"codex.reasoning_effort must be one of: {', '.join(sorted(REASONING_EFFORTS))}")
    configured_allowlist = value(experiment, "allowed_executables", "AUTO_RESEARCH_ALLOWED_EXECUTABLES", None)
    if configured_allowlist is None:
        allowlist = DEFAULT_ALLOWED_EXECUTABLES.copy()
    elif isinstance(configured_allowlist, str):
        allowlist = {item.strip() for item in configured_allowlist.split(",") if item.strip()}
    else:
        allowlist = {str(item).strip() for item in configured_allowlist if str(item).strip()}
    if not allowlist:
        raise ValueError("experiment.allowed_executables must not be empty")

    return HarnessConfig(
        codex_model=model or DEFAULT_CODEX_MODEL,
        codex_reasoning_effort=effort,
        codex_sandbox=str(value(codex, "sandbox", "AUTO_RESEARCH_CODEX_SANDBOX", "danger-full-access")).strip() or "danger-full-access",
        codex_approval=str(value(codex, "approval_policy", "AUTO_RESEARCH_CODEX_APPROVAL", "never")).strip() or "never",
        max_cycles=_int(value(harness, "max_cycles", "AUTO_RESEARCH_MAX_CYCLES", DEFAULT_MAX_CYCLES), "harness.max_cycles", 1),
        reconnect_attempts=_int(value(harness, "reconnect_attempts", "AUTO_RESEARCH_RECONNECT_ATTEMPTS", 3), "harness.reconnect_attempts", 1),
        reconnect_backoff_s=_float(value(harness, "reconnect_backoff_s", "AUTO_RESEARCH_RECONNECT_BACKOFF_S", 2.0), "harness.reconnect_backoff_s"),
        app_server_response_timeout_s=_float(value(harness, "app_server_response_timeout_s", "AUTO_RESEARCH_APP_SERVER_RESPONSE_TIMEOUT_S", 60.0), "harness.app_server_response_timeout_s", 1.0),
        app_server_turn_timeout_s=_float(value(harness, "app_server_turn_timeout_s", "AUTO_RESEARCH_APP_SERVER_TURN_TIMEOUT_S", 900.0), "harness.app_server_turn_timeout_s", 1.0),
        app_server_event_idle_timeout_s=_float(value(harness, "app_server_event_idle_timeout_s", "AUTO_RESEARCH_APP_SERVER_EVENT_IDLE_TIMEOUT_S", 180.0), "harness.app_server_event_idle_timeout_s", 1.0),
        event_poll_s=_float(value(harness, "event_poll_s", "AUTO_RESEARCH_EVENT_POLL_S", 0.25), "harness.event_poll_s"),
        event_grace_s=_float(value(harness, "event_grace_s", "AUTO_RESEARCH_EVENT_GRACE_S", 30.0), "harness.event_grace_s"),
        use_shell=_bool(value(experiment, "use_shell", "AUTO_RESEARCH_USE_SHELL", True), "experiment.use_shell"),
        allowed_executables=frozenset(allowlist),
        default_experiment_timeout_s=_int(value(experiment, "default_timeout_s", "AUTO_RESEARCH_DEFAULT_EXPERIMENT_TIMEOUT_S", 3600), "experiment.default_timeout_s", 1),
        worker_heartbeat_s=_float(value(experiment, "worker_heartbeat_s", "AUTO_RESEARCH_WORKER_HEARTBEAT_S", 5.0), "experiment.worker_heartbeat_s", 0.1),
        one_active_experiment=_bool(value(experiment, "one_active_experiment", "AUTO_RESEARCH_ONE_ACTIVE_EXPERIMENT", True), "experiment.one_active_experiment"),
    )

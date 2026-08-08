"""Safe conversion from user-facing command text to argv."""

from __future__ import annotations

import shlex
from pathlib import Path

from .config import DEFAULT_ALLOWED_EXECUTABLES


def allowed_executables() -> set[str]:
    """Return code defaults for callers without a project config."""
    return DEFAULT_ALLOWED_EXECUTABLES.copy()


def command_to_argv(command: str | list[str] | tuple[str, ...], configured_allowlist: set[str] | frozenset[str] | None = None) -> list[str]:
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    if not argv or not argv[0]:
        raise ValueError("experiment command must not be empty")
    executable = Path(argv[0]).name
    allowlist = configured_allowlist if configured_allowlist is not None else allowed_executables()
    if executable not in allowlist and argv[0] not in allowlist:
        raise ValueError(
            f"executable {argv[0]!r} is not allowed; "
            "configure AUTO_RESEARCH_ALLOWED_EXECUTABLES explicitly"
        )
    return argv

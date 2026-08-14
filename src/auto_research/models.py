from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TerminalRunResult:
    run_id: str
    idea_id: str
    status: str
    return_code: int | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    artifact_validation: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    result_dir: str = ""
    event_path: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""
    command: str = ""
    argv: list[str] = field(default_factory=list)
    worktree: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

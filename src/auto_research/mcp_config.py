"""Install the Experiment MCP registration into a project-local Codex config."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

_EXPERIMENT_SECTION = re.compile(r"(?ms)^\[mcp_servers\.experiment\]\n.*?(?=^\[|\Z)")


def render_mcp_config(
    project_dir: str | Path, python_executable: str | None = None
) -> str:
    """Render the Codex TOML section for the local Experiment MCP server."""
    project = Path(project_dir).resolve()
    # Do not call Path.resolve() here: a venv's python often is a symlink to
    # the base interpreter. Codex must launch the venv entrypoint so that the
    # repository package and optional MCP dependencies are available.
    executable = str(Path(python_executable or sys.executable).absolute())
    # JSON string escaping is compatible with TOML basic strings and handles
    # spaces, quotes, and backslashes in user paths.
    return (
        "[mcp_servers.experiment]\n"
        f"command = {json.dumps(executable)}\n"
        'args = ["-m", "auto_research.mcp_server"]\n'
        f"env = {{ AUTO_RESEARCH_PROJECT_DIR = {json.dumps(str(project))} }}\n"
        "startup_timeout_sec = 20\n"
        "tool_timeout_sec = 120\n"
    )


def register_mcp_config(
    project_dir: str | Path, config_path: str | Path | None = None
) -> Path:
    """Create or update only the Experiment MCP section in Codex config."""
    project = Path(project_dir).resolve()
    target = (
        Path(config_path).resolve()
        if config_path
        else project / ".codex" / "config.toml"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    block = render_mcp_config(project)
    match = _EXPERIMENT_SECTION.search(existing)
    if match:
        content = (
            existing[: match.start()] + block + existing[match.end() :].lstrip("\n")
        )
    else:
        content = existing.rstrip() + ("\n\n" if existing.strip() else "") + block

    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return target

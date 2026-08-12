"""Namespaced durable state locations for independent research sessions."""

from pathlib import Path


def resolve_state_root(project_dir: str | Path, value: str | Path | None = None) -> Path:
    project = Path(project_dir).resolve()
    root = project / "research" if value is None else Path(value)
    if not root.is_absolute():
        root = project / root
    root = root.resolve()
    try:
        root.relative_to(project)
    except ValueError as exc:
        raise ValueError("state_root must be inside the project") from exc
    return root

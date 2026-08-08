from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


VALID_OPERATORS = {">", ">=", "<", "<=", "==", "!="}
DEFAULT_MAX_EXPERIMENTS = 100


@dataclass
class GoalSpec:
    goal_id: str
    statement: str
    primary_metric: str
    direction: str = "maximize"
    secondary_metrics: list[str] = field(default_factory=list)
    baseline_command: str = ""
    baseline_result: str = ""
    editable_paths: list[str] = field(default_factory=list)
    sealed_paths: list[str] = field(default_factory=list)
    max_wall_time_s: int = 3600
    max_experiments: int = DEFAULT_MAX_EXPERIMENTS
    plateau_window: int = 15
    max_consecutive_failures: int = 3
    target_metric: float | None = None
    metric_noise_threshold: float = 0.0
    hard_requirements: list[dict[str, Any]] = field(default_factory=list)
    protocol_requirements: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalSpec":
        optimization = data.get("optimization", {})
        search_space = data.get("search_space", {})
        baseline = data.get("baseline", {})
        constraints = data.get("constraints", {})
        stopping = data.get("stopping", {})
        hard_requirements = data.get("hard_requirements", constraints.get("hard_requirements", []))
        if not isinstance(hard_requirements, list):
            raise ValueError("hard_requirements must be a list")
        protocol_requirements = data.get("protocol_requirements", constraints.get("protocol_requirements", []))
        if not isinstance(protocol_requirements, list) or not all(isinstance(item, str) for item in protocol_requirements):
            raise ValueError("protocol_requirements must be a list of strings")
        for requirement in hard_requirements:
            if not isinstance(requirement, dict):
                raise ValueError("each hard requirement must be an object")
            if not isinstance(requirement.get("metric"), str):
                raise ValueError("hard requirement metric must be a string")
            operator = requirement.get("operator", requirement.get("op", ">="))
            if operator not in VALID_OPERATORS:
                raise ValueError(f"unsupported hard requirement operator: {operator}")
            value = requirement.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError("hard requirement value must be numeric")
            when = requirement.get("when", {})
            if not isinstance(when, dict):
                raise ValueError("hard requirement when must be an object")
        return cls(
            goal_id=data["goal_id"],
            statement=data["statement"],
            primary_metric=data["primary_metric"],
            direction=data.get("direction", "maximize"),
            secondary_metrics=data.get("secondary_metrics", []),
            baseline_command=baseline.get("command", data.get("baseline_command", "")),
            baseline_result=baseline.get("result", data.get("baseline_result", "")),
            editable_paths=search_space.get("editable_paths", data.get("editable_paths", [])),
            sealed_paths=search_space.get("sealed_paths", data.get("sealed_paths", [])),
            max_wall_time_s=constraints.get("max_wall_time_s", 3600),
            max_experiments=stopping.get("max_experiments", DEFAULT_MAX_EXPERIMENTS),
            plateau_window=stopping.get("plateau_window", 15),
            max_consecutive_failures=stopping.get("max_consecutive_failures", 3),
            target_metric=stopping.get("target_metric"),
            metric_noise_threshold=stopping.get("metric_noise_threshold", 0.0),
            hard_requirements=hard_requirements,
            protocol_requirements=protocol_requirements,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TerminalRunResult:
    run_id: str
    idea_id: str
    status: str
    return_code: int | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
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

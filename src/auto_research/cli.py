from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import DEFAULT_MAX_EXPERIMENTS


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auto-research")
    sub = parser.add_subparsers(dest="action", required=True)

    init = sub.add_parser("init", help="create goal.json and research/config.toml")
    init.add_argument("project", nargs="?", default=".")

    session = sub.add_parser(
        "session",
        help="create once or reuse the project's dedicated Codex research task",
    )
    session.add_argument("--project", default=".")
    session.add_argument(
        "--create-thread",
        action="store_true",
        help="create a project-bound task only when no binding exists",
    )
    session.add_argument(
        "--thread-id",
        help="bind an existing project task instead of creating one",
    )
    session.add_argument("--title")
    session.add_argument("--objective")
    session.add_argument(
        "--replace-goal",
        action="store_true",
        help="allow --objective to replace an existing different Goal",
    )

    start = sub.add_parser(
        "start", help="start a detached experiment and arm Goal wake-up"
    )
    start.add_argument("--project", default=".")
    start.add_argument("--idea-id", required=True)
    start.add_argument("--worktree", default=".")
    start.add_argument("--command", required=True)
    start.add_argument("--timeout-s", type=int)
    start.add_argument("--idempotency-key")
    start.add_argument("--thread-id")

    status = sub.add_parser("status", help="show durable run and wake-listener state")
    status.add_argument("run_id")
    status.add_argument("--project", default=".")

    wait = sub.add_parser("wait", help="wait locally for a durable run terminal event")
    wait.add_argument("run_id")
    wait.add_argument("--project", default=".")

    arm = sub.add_parser("arm-wake", help="arm or run the one-shot Goal wake listener")
    arm.add_argument("run_id")
    arm.add_argument("--project", default=".")
    arm.add_argument("--thread-id")
    arm.add_argument("--foreground", action="store_true")

    recover = sub.add_parser("recover-wakes", help="restart unfinished wake listeners")
    recover.add_argument("--project", default=".")

    mcp = sub.add_parser("mcp-server", help="run the optional experiment MCP server")
    mcp.add_argument("--project", default=".")

    mcp_config = sub.add_parser(
        "print-mcp-config", help="print Codex MCP configuration"
    )
    mcp_config.add_argument("--project", default=".")

    register_mcp = sub.add_parser(
        "register-mcp",
        help="register or update the optional Experiment MCP server",
    )
    register_mcp.add_argument("--project", default=".")

    supervisor = sub.add_parser(
        "supervisor", help="run or inspect the single-writer App Server scheduler"
    )
    supervisor_sub = supervisor.add_subparsers(dest="supervisor_action", required=True)
    supervisor_run = supervisor_sub.add_parser("run", help="run in the foreground")
    supervisor_run.add_argument("--project", default=".")
    supervisor_run.add_argument("--max-turns", type=int)
    supervisor_start = supervisor_sub.add_parser(
        "start", help="start a detached Supervisor process"
    )
    supervisor_start.add_argument("--project", default=".")
    supervisor_status = supervisor_sub.add_parser(
        "status", help="show durable Supervisor state"
    )
    supervisor_status.add_argument("--project", default=".")
    supervisor_resume = supervisor_sub.add_parser(
        "resume", help="move an operator-paused Supervisor back to TURN_READY"
    )
    supervisor_resume.add_argument("--project", default=".")

    args = parser.parse_args(argv)
    if args.action == "init":
        root = Path(args.project).resolve()
        goal_path = root / "goal.json"
        goal_path.parent.mkdir(parents=True, exist_ok=True)
        if not goal_path.exists():
            goal_path.write_text(
                json.dumps(
                    {
                        "goal_id": "goal-001",
                        "statement": "Improve the primary metric under fixed evaluation constraints.",
                        "primary_metric": "score",
                        "direction": "maximize",
                        "baseline": {"command": "", "result": ""},
                        "search_space": {
                            "editable_paths": ["src/"],
                            "sealed_paths": ["data/", "eval/"],
                        },
                        "constraints": {"max_wall_time_s": 3600},
                        "hard_requirements": [],
                        "stopping": {
                            "max_experiments": DEFAULT_MAX_EXPERIMENTS,
                            "plateau_window": 15,
                            "max_consecutive_failures": 3,
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        config_path = root / "research" / "config.toml"
        if not config_path.exists():
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                "[listener]\nauto_wake = true\napp_server_response_timeout_s = 60.0\n"
                "bind_recency_s = 600.0\n"
                "reconnect_initial_s = 2.0\nreconnect_max_s = 60.0\n"
                "event_poll_s = 0.25\nevent_grace_s = 30.0\n\n"
                '[experiment]\nuse_shell = true\nallowed_executables = ["python", "python3"]\n'
                "default_timeout_s = 3600\nworker_heartbeat_s = 5.0\n"
                "one_active_experiment = true\n",
                encoding="utf-8",
            )
        _print({"goal": str(goal_path), "config": str(config_path)})
        return 0

    if args.action == "session":
        from .research_session import ResearchSessionManager

        result = ResearchSessionManager(args.project).prepare(
            create_thread=args.create_thread,
            thread_id=args.thread_id,
            objective=args.objective,
            title=args.title,
            replace_goal=args.replace_goal,
        )
        _print(result)
        return 0

    if args.action == "start":
        from .mcp_server import ExperimentService

        service = ExperimentService(args.project)
        result = service.start_experiment(
            args.idea_id,
            args.worktree,
            args.command,
            args.timeout_s,
            idempotency_key=args.idempotency_key,
            thread_id=args.thread_id,
        )
        _print(result)
        return 0

    if args.action in {"status", "wait"}:
        from .ledger import read_json
        from .runner import ExperimentRunner

        project = Path(args.project).resolve()
        runner = ExperimentRunner(project / "research" / "runs")
        if args.action == "wait":
            result = runner.wait(args.run_id)
            _print(result.to_dict())
            return 0 if result.status == "COMPLETED" else 1
        result = runner.get_result(args.run_id)
        run_dir = runner.runs_dir / args.run_id
        _print(
            {
                "run": runner.get_run(args.run_id),
                "wake": read_json(run_dir / "wake.json", None),
                "result": result.to_dict() if result else None,
            }
        )
        return 0

    if args.action == "arm-wake":
        from .wake_listener import GoalWakeListener, spawn_wake_listener

        if args.foreground:
            result = GoalWakeListener(
                args.project,
                args.run_id,
                thread_id=args.thread_id,
            ).run()
        else:
            result = spawn_wake_listener(
                args.project,
                args.run_id,
                thread_id=args.thread_id,
            )
        _print(result)
        return 0

    if args.action == "recover-wakes":
        from .wake_listener import recover_wake_listeners

        _print(recover_wake_listeners(args.project))
        return 0

    if args.action == "mcp-server":
        import os

        from .mcp_server import main as mcp_main

        os.environ["AUTO_RESEARCH_PROJECT_DIR"] = str(Path(args.project).resolve())
        mcp_main()
        return 0

    if args.action == "print-mcp-config":
        from .mcp_config import render_mcp_config

        print(render_mcp_config(args.project), end="")
        return 0

    if args.action == "register-mcp":
        from .mcp_config import register_mcp_config

        print(register_mcp_config(args.project))
        return 0

    if args.action == "supervisor":
        from .supervisor import (
            AppServerSupervisor,
            read_supervisor_state,
            spawn_supervisor,
        )

        if args.supervisor_action == "run":
            result = AppServerSupervisor(args.project).run(max_turns=args.max_turns)
        elif args.supervisor_action == "start":
            result = spawn_supervisor(args.project)
        elif args.supervisor_action == "resume":
            result = AppServerSupervisor(args.project).resume()
        else:
            result = read_supervisor_state(args.project) or {"state": "NOT_STARTED"}
        _print(result)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

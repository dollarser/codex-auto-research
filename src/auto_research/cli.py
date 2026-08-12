from __future__ import annotations

import argparse
import json
import os
import time
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
    session.add_argument("--state-root")
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

    submit = sub.add_parser(
        "submit", help="submit an experiment command for Supervisor to launch"
    )
    submit.add_argument("--project", default=".")
    submit.add_argument("--state-root")
    submit.add_argument("--idea-id", required=True)
    submit.add_argument("--worktree", default=".")
    submit.add_argument("--command", required=True)
    submit.add_argument("--timeout-s", type=int)
    submit.add_argument("--idempotency-key")
    submit.add_argument("--thread-id")

    status = sub.add_parser("status", help="show durable run and wake-listener state")
    status.add_argument("run_id")
    status.add_argument("--project", default=".")
    status.add_argument("--state-root")

    wait = sub.add_parser("wait", help="wait locally for a durable run terminal event")
    wait.add_argument("run_id")
    wait.add_argument("--project", default=".")
    wait.add_argument("--state-root")

    goal = sub.add_parser(
        "goal",
        help="change the managed research Goal state from its own Goal task",
    )
    goal_sub = goal.add_subparsers(dest="goal_action", required=True)
    goal_status = goal_sub.add_parser(
        "set-status", help="set the current managed Goal status"
    )
    goal_status.add_argument("status", choices=("active", "paused", "blocked", "complete"))
    goal_status.add_argument("--project", default=".")
    goal_status.add_argument("--state-root")
    goal_status.add_argument(
        "--thread-id",
        help="only needed when CODEX_THREAD_ID is not available to the Goal shell",
    )

    arm = sub.add_parser(
        "arm-wake",
        help="[legacy Desktop only] explicitly arm the one-shot Goal listener",
    )
    arm.add_argument("run_id")
    arm.add_argument("--project", default=".")
    arm.add_argument("--thread-id")
    arm.add_argument("--foreground", action="store_true")

    recover = sub.add_parser(
        "recover-wakes",
        help="[legacy Desktop only] recover listeners when auto_wake=true",
    )
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
        "supervisor", help="run or inspect the native Goal experiment monitor"
    )
    supervisor_sub = supervisor.add_subparsers(dest="supervisor_action", required=True)
    supervisor_run = supervisor_sub.add_parser("run", help="run in the foreground")
    supervisor_run.add_argument("--project", default=".")
    supervisor_run.add_argument("--state-root")
    supervisor_run.add_argument(
        "--session-mode", choices=("auto", "dedicated", "adopted"), default="auto"
    )
    supervisor_start = supervisor_sub.add_parser(
        "start", help="start a detached Supervisor process"
    )
    supervisor_start.add_argument("--project", default=".")
    supervisor_start.add_argument("--state-root")
    supervisor_start.add_argument(
        "--session-mode", choices=("auto", "dedicated", "adopted"), default="auto"
    )
    supervisor_status = supervisor_sub.add_parser(
        "status", help="show durable Supervisor state"
    )
    supervisor_status.add_argument("--project", default=".")
    supervisor_status.add_argument("--state-root")
    supervisor_resume = supervisor_sub.add_parser(
        "resume", help="move an operator-paused Supervisor back to TURN_READY"
    )
    supervisor_resume.add_argument("--project", default=".")
    supervisor_resume.add_argument("--state-root")
    supervisor_resume.add_argument(
        "--session-mode", choices=("auto", "dedicated", "adopted"), default="auto"
    )
    supervisor_restart = supervisor_sub.add_parser(
        "restart", help="reuse a completed session with an explicitly new Goal"
    )
    supervisor_restart.add_argument("--project", default=".")
    supervisor_restart.add_argument("--state-root")
    supervisor_restart.add_argument(
        "--session-mode", choices=("auto", "dedicated", "adopted"), default="auto"
    )
    supervisor_restart.add_argument("--objective", required=True)
    supervisor_restart.add_argument("--title")

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
                '[codex]\nmodel = "gpt-5.6-terra"\n'
                'approval_policy = "never"\nsandbox = "workspace-write"\n\n'
                "# Legacy Desktop compatibility; main uses the App Server Supervisor.\n"
                "[listener]\nauto_wake = false\napp_server_response_timeout_s = 60.0\n"
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

        result = ResearchSessionManager(args.project, state_root=args.state_root).prepare(
            create_thread=args.create_thread,
            thread_id=args.thread_id,
            objective=args.objective,
            title=args.title,
            replace_goal=args.replace_goal,
        )
        _print(result)
        return 0

    if args.action == "submit":
        from .mcp_server import ExperimentService

        service = ExperimentService(args.project, state_root=args.state_root)
        result = service.submit_experiment(
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

        from .state_paths import resolve_state_root
        project = Path(args.project).resolve()
        runner = ExperimentRunner(resolve_state_root(project, args.state_root) / "runs")
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

    if args.action == "goal":
        from .app_server import AppServerClient
        from .ledger import read_json, write_json_atomic
        from .research_session import read_bound_thread_id
        from .supervisor import is_supervisor_thread, supervisor_active_experiment_path

        project = Path(args.project).resolve()
        environment_thread_id = os.environ.get("CODEX_THREAD_ID") or None
        bound_thread_id = read_bound_thread_id(project, args.state_root)
        requested_thread_id = args.thread_id or environment_thread_id or bound_thread_id
        if not requested_thread_id:
            raise ValueError("no managed Goal thread is bound to this project")
        if environment_thread_id and requested_thread_id != environment_thread_id:
            raise ValueError("--thread-id does not match current CODEX_THREAD_ID")
        if not is_supervisor_thread(project, requested_thread_id, args.state_root):
            raise ValueError("--thread-id is not this project's managed Supervisor Goal thread")
        handoff: dict[str, object] | None = None
        if args.status == "paused":
            # Store the wait handoff before changing Goal state.  Supervisor
            # polls this marker while the Goal Turn is still in progress.
            marker_path = supervisor_active_experiment_path(project, args.state_root)
            marker = read_json(marker_path, {}) or {}
            if marker.get("thread_id") == requested_thread_id and isinstance(
                marker.get("run_id"), str
            ):
                marker.update(
                    {
                        "wait_requested": True,
                        "wait_requested_at": time.time(),
                        "wait_requested_by": "goal_self_control",
                    }
                )
                write_json_atomic(marker_path, marker)
                handoff = {
                    "run_id": marker["run_id"],
                    "wait_requested": True,
                    "owner": "supervisor",
                }
        request_path = (
            supervisor_active_experiment_path(project, args.state_root).parent
            / "goal_status_request.json"
        )
        request = {
            "thread_id": requested_thread_id,
            "status": args.status,
            "requested_at": time.time(),
            "requested_by": "goal_self_control",
        }
        try:
            with AppServerClient(
                project,
                client_name="auto-research-goal-self-control",
                client_version="0.6.0",
                managed_daemon=True,
                ensure_daemon=False,
            ) as client:
                client.initialize()
                result = client.set_goal_status(requested_thread_id, args.status)
            _print({**result, "experiment_handoff": handoff, "bridge": "direct"})
        except Exception as exc:  # The Goal shell may not read daemon pid locks.
            write_json_atomic(request_path, request)
            _print(
                {
                    "threadId": requested_thread_id,
                    "status": "PENDING_SUPERVISOR",
                    "requested_status": args.status,
                    "experiment_handoff": handoff,
                    "bridge": "supervisor",
                    "direct_error": f"{type(exc).__name__}: {exc}",
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
            restart_supervisor,
            spawn_supervisor,
        )

        if args.supervisor_action == "run":
            result = AppServerSupervisor(
                args.project,
                session_mode=args.session_mode,
                state_root=args.state_root,
            ).run()
        elif args.supervisor_action == "start":
            result = spawn_supervisor(
                args.project,
                session_mode=args.session_mode,
                state_root=args.state_root,
            )
        elif args.supervisor_action == "resume":
            result = AppServerSupervisor(
                args.project,
                session_mode=args.session_mode,
                state_root=args.state_root,
            ).resume()
            result["supervisor"] = spawn_supervisor(
                args.project,
                session_mode=args.session_mode,
                state_root=args.state_root,
            )
        elif args.supervisor_action == "restart":
            result = restart_supervisor(
                args.project,
                objective=args.objective,
                title=args.title,
                session_mode=args.session_mode,
                state_root=args.state_root,
            )
        else:
            result = read_supervisor_state(args.project, args.state_root) or {"state": "NOT_STARTED"}
        _print(result)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

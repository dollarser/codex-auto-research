from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auto-research")
    sub = parser.add_subparsers(dest="action", required=True)

    init = sub.add_parser("init", help="create GOAL.md and research/config.toml")
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
        "--creation-key",
        help="stable idempotency key required with --create-thread",
    )
    session.add_argument(
        "--replace-goal",
        action="store_true",
        help="allow --objective to replace an existing different Goal",
    )

    submit = sub.add_parser(
        "submit", help="submit an experiment command for Supervisor to launch"
    )
    submit.add_argument("--project", default=".")
    submit.add_argument("--idea-id", required=True)
    submit.add_argument("--worktree", default=".")
    submit.add_argument("--command", required=True)
    submit.add_argument("--timeout-s", type=int)
    submit.add_argument("--idempotency-key")
    submit.add_argument("--thread-id")
    submit.add_argument(
        "--gpu-ids",
        help="comma-separated advisory GPU claim persisted with the run",
    )
    submit.add_argument(
        "--expected-artifact",
        action="append",
        default=[],
        help="repeatable artifact path to validate after process completion",
    )

    status = sub.add_parser("status", help="show durable experiment state")
    status.add_argument("run_id")
    status.add_argument("--project", default=".")
    status.add_argument("--thread-id")

    wait = sub.add_parser("wait", help="wait locally for a durable run terminal event")
    wait.add_argument("run_id")
    wait.add_argument("--project", default=".")
    wait.add_argument("--thread-id")

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
    goal_status.add_argument(
        "--thread-id",
        help="only needed when CODEX_THREAD_ID is not available to the Goal shell",
    )

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
    supervisor_run.add_argument("--thread-id")
    supervisor_start = supervisor_sub.add_parser(
        "start", help="start a detached Supervisor process"
    )
    supervisor_start.add_argument("--project", default=".")
    supervisor_start.add_argument("--thread-id")
    supervisor_status = supervisor_sub.add_parser(
        "status", help="show durable Supervisor state"
    )
    supervisor_status.add_argument("--project", default=".")
    supervisor_status.add_argument("--thread-id")
    supervisor_resume = supervisor_sub.add_parser(
        "resume", help="retry a Supervisor stopped in NEEDS_USER"
    )
    supervisor_resume.add_argument("--project", default=".")
    supervisor_resume.add_argument("--thread-id")
    supervisor_restart = supervisor_sub.add_parser(
        "restart", help="reuse a completed session with an explicitly new Goal"
    )
    supervisor_restart.add_argument("--project", default=".")
    supervisor_restart.add_argument("--thread-id")
    supervisor_restart.add_argument("--objective", required=True)
    supervisor_restart.add_argument("--title")

    args = parser.parse_args(argv)
    if args.action == "init":
        root = Path(args.project).resolve()
        goal_path = root / "GOAL.md"
        goal_path.parent.mkdir(parents=True, exist_ok=True)
        if not goal_path.exists():
            goal_path.write_text(
                "# Goal\n\n"
                "Improve the primary metric under fixed evaluation constraints.\n\n"
                "## Success criteria\n\n"
                "- Define measurable acceptance criteria here.\n\n"
                "## Constraints\n\n"
                "- Record fixed data, evaluation, resource, and safety boundaries here.\n",
                encoding="utf-8",
            )
        config_path = root / "research" / "config.toml"
        if not config_path.exists():
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                '[codex]\nmodel = "gpt-5.6-terra"\n'
                'approval_policy = "never"\nsandbox = "workspace-write"\n\n'
                "[supervisor]\napp_server_response_timeout_s = 60.0\n"
                "goal_turn_timeout_s = 1800.0\n"
                "event_poll_s = 0.25\nevent_grace_s = 30.0\n\n"
                "[experiment]\ndefault_timeout_s = 3600\nworker_heartbeat_s = 5.0\n",
                encoding="utf-8",
            )
        _print({"goal": str(goal_path), "config": str(config_path)})
        return 0

    if args.action == "session":
        from .research_session import ResearchSessionManager

        result = ResearchSessionManager(
            args.project,
            thread_id=args.thread_id,
        ).prepare(
            create_thread=args.create_thread,
            thread_id=args.thread_id,
            objective=args.objective,
            title=args.title,
            replace_goal=args.replace_goal,
            creation_key=args.creation_key,
        )
        _print(result)
        return 0

    if args.action == "submit":
        from .mcp_server import ExperimentService

        service = ExperimentService(args.project, thread_id=args.thread_id)
        gpu_ids = None
        if args.gpu_ids:
            gpu_ids = [int(value.strip()) for value in args.gpu_ids.split(",")]
        result = service.submit_experiment(
            args.idea_id,
            args.worktree,
            args.command,
            args.timeout_s,
            idempotency_key=args.idempotency_key,
            thread_id=args.thread_id,
            gpu_ids=gpu_ids,
            expected_artifacts=args.expected_artifact,
        )
        _print(result)
        return 0

    if args.action in {"status", "wait"}:
        from .runner import ExperimentRunner
        from .state_paths import thread_state_root
        project = Path(args.project).resolve()
        runner = ExperimentRunner(
            thread_state_root(project, args.thread_id) / "runs"
        )
        if args.action == "wait":
            result = runner.wait(args.run_id)
            _print(result.to_dict())
            return 0 if result.status == "COMPLETED" else 1
        result = runner.get_result(args.run_id)
        _print(
            {
                "run": runner.get_run(args.run_id),
                "result": result.to_dict() if result else None,
            }
        )
        return 0

    if args.action == "goal":
        from .app_server import AppServerClient
        from .ledger import write_json_atomic
        from .state_paths import resolve_thread_id, thread_state_root
        from .supervisor import is_supervisor_thread, supervisor_dir

        project = Path(args.project).resolve()
        requested_thread_id = resolve_thread_id(args.thread_id)
        if not is_supervisor_thread(project, requested_thread_id):
            raise ValueError("--thread-id is not this project's managed Supervisor Goal thread")
        request_id = uuid.uuid4().hex
        request_path = (
            supervisor_dir(project, requested_thread_id)
            / "goal_status_requests"
            / f"{time.time_ns()}-{request_id}.json"
        )
        request = {
            "request_id": request_id,
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
            _print({**result, "bridge": "direct"})
        except Exception as exc:  # noqa: BLE001 - bridge any transport/runtime failure
            write_json_atomic(request_path, request)
            _print(
                {
                    "threadId": requested_thread_id,
                    "status": "PENDING_SUPERVISOR",
                    "requested_status": args.status,
                    "bridge": "supervisor",
                    "direct_error": f"{type(exc).__name__}: {exc}",
                }
            )
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
        )
        from .supervisor_process import (
            read_supervisor_process,
            restart_supervisor,
            spawn_supervisor,
        )

        if args.supervisor_action == "run":
            result = AppServerSupervisor(
                args.project,
                thread_id=args.thread_id,
                allow_limited_retry=True,
            ).run()
        elif args.supervisor_action == "start":
            result = spawn_supervisor(
                args.project,
                thread_id=args.thread_id,
                retry_limited=True,
            )
        elif args.supervisor_action == "resume":
            result = AppServerSupervisor(
                args.project,
                thread_id=args.thread_id,
            ).resume()
            result["supervisor"] = spawn_supervisor(
                args.project,
                thread_id=args.thread_id,
                retry_limited=True,
            )
        elif args.supervisor_action == "restart":
            result = restart_supervisor(
                args.project,
                objective=args.objective,
                title=args.title,
                thread_id=args.thread_id,
            )
        else:
            from .state_paths import resolve_thread_id, validate_thread_id

            thread_id = (
                validate_thread_id(args.thread_id)
                if args.thread_id is not None
                else resolve_thread_id()
            )
            result = read_supervisor_state(args.project, thread_id) or {
                "state": "NOT_STARTED"
            }
            result = {
                **result,
                "process": read_supervisor_process(args.project, thread_id),
            }
        _print(result)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

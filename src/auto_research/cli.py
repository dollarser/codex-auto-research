from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runner import ExperimentRunner
from .config import DEFAULT_MAX_CYCLES
from .models import DEFAULT_MAX_EXPERIMENTS

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auto-research")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a sample goal.json")
    init.add_argument("project", nargs="?", default=".")

    wait = sub.add_parser("wait", help="recover and wait for a durable run")
    wait.add_argument("run_id")
    wait.add_argument("--runs-dir", default="research/runs")

    mcp = sub.add_parser("mcp-server", help="run the experiment MCP server")
    mcp.add_argument("--project", default=".")

    mcp_config = sub.add_parser("print-mcp-config", help="print Codex MCP configuration")
    mcp_config.add_argument("--project", default=".")

    register_mcp = sub.add_parser(
        "register-mcp",
        help="register or update the Experiment MCP server in .codex/config.toml",
    )
    register_mcp.add_argument("--project", default=".")

    harness = sub.add_parser("goal-harness", help="run event-driven Codex Goal recovery")
    harness.add_argument("--project", default=".")
    harness.add_argument("--objective", required=True)
    harness.add_argument("--prompt")
    harness.add_argument("--prompt-file")
    harness.add_argument("--max-cycles", type=int, default=None)
    harness.add_argument(
        "--fresh-thread",
        action="store_true",
        help=(
            "explicitly replace the persisted Codex thread while preserving research state; "
            "do not use for automatic restart/recovery"
        ),
    )

    args = parser.parse_args(argv)
    if args.command == "init":
        path = Path(args.project) / "goal.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "goal_id": "goal-001",
            "statement": "Improve the primary algorithm metric under fixed evaluation and resource constraints.",
            "primary_metric": "score",
            "direction": "maximize",
            "baseline": {"command": "", "result": ""},
            "search_space": {"editable_paths": ["src/"], "sealed_paths": ["data/", "eval/"]},
            "constraints": {"max_wall_time_s": 300},
            "hard_requirements": [],
            "stopping": {"max_experiments": DEFAULT_MAX_EXPERIMENTS, "plateau_window": 15, "max_consecutive_failures": 3},
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        harness_config = path.parent / "research" / "harness.toml"
        if not harness_config.exists():
            harness_config.parent.mkdir(parents=True, exist_ok=True)
            harness_config.write_text(
                '[codex]\nmodel = "gpt-5.6-luna"\nreasoning_effort = "medium"\n'
                'sandbox = "danger-full-access"\napproval_policy = "never"\n\n'
                f'[harness]\nmax_cycles = {DEFAULT_MAX_CYCLES}\nreconnect_attempts = 3\n'
                'reconnect_backoff_s = 2.0\napp_server_response_timeout_s = 60.0\n'
                'app_server_turn_timeout_s = 900.0\nevent_poll_s = 0.25\nevent_grace_s = 30.0\n\n'
                '[experiment]\nuse_shell = true\nallowed_executables = ["python", "python3"]\n'
                'default_timeout_s = 3600\nworker_heartbeat_s = 5.0\n',
                encoding="utf-8",
            )
        print(path)
        return 0

    if args.command == "wait":
        result = ExperimentRunner(args.runs_dir).wait(args.run_id)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.status == "COMPLETED" else 1

    if args.command == "mcp-server":
        from .mcp_server import main as mcp_main
        import os

        os.environ["AUTO_RESEARCH_PROJECT_DIR"] = str(Path(args.project).resolve())
        mcp_main()
        return 0

    if args.command == "print-mcp-config":
        from .mcp_config import render_mcp_config

        print(render_mcp_config(args.project), end="")
        return 0

    if args.command == "register-mcp":
        from .mcp_config import register_mcp_config

        print(register_mcp_config(args.project))
        return 0

    if args.command == "goal-harness":
        if bool(args.prompt) == bool(args.prompt_file):
            raise SystemExit("Provide exactly one of --prompt or --prompt-file")
        prompt = args.prompt
        if args.prompt_file:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        from .goal_harness import GoalHarness

        result = GoalHarness(args.project).run(args.objective, prompt, args.max_cycles, args.fresh_thread)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

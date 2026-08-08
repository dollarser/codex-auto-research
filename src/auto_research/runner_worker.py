from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .ledger import read_json, write_json_atomic
from .runner import TERMINAL_EVENT_NAMES, _terminal_lock, finalize_run


def _write_event(run_dir: Path, name: str, payload: dict) -> None:
    write_json_atomic(run_dir / "events" / name, payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    run_dir = Path(args.run_dir).resolve()
    run = read_json(run_dir / "run.json", {})
    run_id = run["run_id"]
    idea_id = run["idea_id"]
    timeout_s = int(run["timeout_s"])
    command = run.get("command") if run.get("shell", True) else run.get("argv")
    if not command:
        raise RuntimeError("run.json does not contain validated argv")
    worktree = run["worktree"]
    started = {
        "event": "RUN_STARTED",
        "run_id": run_id,
        "idea_id": idea_id,
        "started_at": time.time(),
    }
    _write_event(run_dir, "started.json", started)

    heartbeat_stop = threading.Event()

    heartbeat_interval_s = float(run.get("worker_heartbeat_s", 5.0))

    def heartbeat() -> None:
        while not heartbeat_stop.wait(heartbeat_interval_s):
            write_json_atomic(
                run_dir / "heartbeat.json",
                {
                    "run_id": run_id,
                    "worker_pid": os.getpid(),
                    "timestamp": time.time(),
                },
            )

    write_json_atomic(
        run_dir / "heartbeat.json",
        {
            "run_id": run_id,
            "worker_pid": os.getpid(),
            "timestamp": time.time(),
        },
    )
    threading.Thread(target=heartbeat, daemon=True).start()

    # Cancellation takes the same lock while writing the cancellation event.
    # Holding it through Popen and child_pid persistence prevents a cancel
    # request from racing between the preflight check and process creation.
    with _terminal_lock(run_dir):
        current = read_json(run_dir / "run.json", run)
        if any((run_dir / "events" / name).exists() for name in TERMINAL_EVENT_NAMES):
            heartbeat_stop.set()
            return 0
        if (run_dir / "cancel.requested.json").exists() or current.get(
            "status"
        ) == "CANCELLED":
            heartbeat_stop.set()
            return 0
        stdout_file = (run_dir / "stdout.log").open("ab")
        stderr_file = (run_dir / "stderr.log").open("ab")
        try:
            child = subprocess.Popen(
                command,
                cwd=worktree,
                shell=bool(run.get("shell", True)),
                stdout=stdout_file,
                stderr=stderr_file,
                env={**os.environ, **(run.get("env") or {})},
                start_new_session=True,
            )
        except OSError as exc:
            heartbeat_stop.set()
            event = {
                "event": "RUN_FAILED",
                "run_id": run_id,
                "idea_id": idea_id,
                "status": "FAILED",
                "return_code": None,
                "error": f"could not start experiment: {exc}",
                "finished_at": time.time(),
            }
            write_json_atomic(run_dir / "run.json", {**run, "status": "FAILED"})
            write_json_atomic(run_dir / "events" / "failed.json", event)
            return 1
        finally:
            stdout_file.close()
            stderr_file.close()
        write_json_atomic(
            run_dir / "run.json", {**run, "child_pid": child.pid, "status": "RUNNING"}
        )
    try:
        return_code = child.wait(timeout=timeout_s)
        if return_code == 0:
            status = "COMPLETED"
            event_name = "completed.json"
            error = ""
            try:
                metrics = json.loads(
                    (run_dir / "metrics.json").read_text(encoding="utf-8")
                )
                if not isinstance(metrics, dict) or not metrics:
                    raise ValueError(
                        "metrics.json must contain a non-empty JSON object"
                    )
            except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
                status = "FAILED"
                event_name = "failed.json"
                error = f"invalid experiment result: {exc}"
        else:
            status = "FAILED"
            event_name = "failed.json"
            error = f"experiment exited with code {return_code}"
    except subprocess.TimeoutExpired:
        status = "TIMEOUT"
        event_name = "timeout.json"
        error = f"experiment exceeded {timeout_s}s"
        cleanup_errors: list[str] = []
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=10)
        except (
            ProcessLookupError,
            PermissionError,
            subprocess.TimeoutExpired,
            OSError,
        ) as exc:
            cleanup_errors.append(f"SIGTERM cleanup failed: {exc}")
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError) as kill_exc:
                cleanup_errors.append(f"SIGKILL cleanup failed: {kill_exc}")
        if cleanup_errors:
            error += "; " + "; ".join(cleanup_errors)
        return_code = None

    heartbeat_stop.set()
    event = {
        "event": f"RUN_{status}",
        "run_id": run_id,
        "idea_id": idea_id,
        "status": status,
        "return_code": return_code,
        "error": error,
        "finished_at": time.time(),
    }
    finalize_run(run_dir, event_name, event, run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

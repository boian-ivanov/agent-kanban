#!/usr/bin/env python3
"""Orchestrator loop for agent-kanban (user-side verification + dispatch).

Based on the working pattern from 2026-08-16 (T-060 close / T-061 dispatch).

Flow:
  1. (optional) Comment on a finished task + move it to `done` (verified work).
  2. (optional) Dispatch the next task: move to `approved` (trigger fires launcher).
  3. Watch a task in 10-minute windows (churn guard): `--watch` returns on
     settle or after the window; at a window boundary run `--progress` —
     alive + log growing → re-watch another 10 min; dead or silent → kill
     the agent and restart the task (comment + backlog → approved).

Usage:
  # Close a verified task, dispatch the next one, then monitor it:
  python3 orchestrator.py --close T-060 --close-comment "PASS (code) ..." \\
      --next T-061 --watch T-061

  # Dispatch + monitor only:
  python3 orchestrator.py --next T-061 --watch T-061

  # Monitor only:
  python3 orchestrator.py --watch T-074

Settled states: testing, uat, done, blocked, cancelled.
"""
import argparse
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:7777"
LOG = "/Users/boian.ivanov/Library/Logs/agent-kanban/launcher.log"
AGENT_LOG_DIR = "/Users/boian.ivanov/Projects/agent-kanban/kanban_data/agent-logs"
SETTLED = ("testing", "uat", "done", "blocked", "cancelled")
POLL_S = 20
# Churn guard (2026-08-23): watch in 10-minute windows; the loop checks
# `--progress` at each window boundary. Healthy (alive + log growing) →
# re-watch another 10 min. Churn (process dead, or no output in the
# window) → kill + restart with a comment. Budgets apply uniformly.
DEFAULT_TIMEOUT_S = 600  # 10 min


def req(method: str, path: str, obj: dict | None = None) -> dict:
    data = json.dumps(obj).encode() if obj is not None else None
    r = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read().decode())


def progress(task_id: str, prev_bytes: int | None = None) -> int:
    """Churn check for a running task at a 10-min window boundary. Prints
    evidence, exits 0 = healthy (keep watching), 1 = churn (kill + restart).

    Signals (either one trips churn):
      - process dead while the task is still in_progress
      - no agent-log growth across the window while the process is alive
        (stalled without output — T-284 burned 500M tokens this way)
    """
    import os
    import subprocess

    task = req("GET", f"/api/tasks/{task_id}")
    status = task.get("status", "")

    moved = task.get("moved_at")
    elapsed_min = 0.0
    if moved:
        try:
            from datetime import datetime, timezone

            moved_dt = datetime.fromisoformat(moved.replace("Z", "+00:00"))
            elapsed_min = (datetime.now(timezone.utc) - moved_dt).total_seconds() / 60
        except ValueError:
            pass

    log_path = os.path.join(AGENT_LOG_DIR, f"{task_id}.log")
    log_bytes = os.path.getsize(log_path) if os.path.exists(log_path) else 0

    alive = (
        subprocess.run(
            ["pgrep", "-f", f"--no-session.*{task_id}"],
            capture_output=True,
        ).returncode
        == 0
    )

    print(f"[progress] {task_id} status={status} "
          f"elapsed={elapsed_min:.0f}m "
          f"log={log_bytes}B (prev {prev_bytes}) alive={alive}")

    if status not in ("in_progress", "approved", "analyst"):
        print("[progress] settled — no churn check needed")
        return 0

    reasons = []
    if not alive:
        reasons.append("process dead")
    if prev_bytes is not None and alive and log_bytes <= prev_bytes:
        reasons.append(f"no output in window (log {log_bytes}B)")

    if reasons:
        print(f"[progress] CHURN: {'; '.join(reasons)}")
        return 1
    print("[progress] healthy")
    return 0



def close_task(task_id: str, comment: str | None) -> None:
    if comment:
        req("POST", f"/api/tasks/{task_id}/comment", {"text": comment})
    req("POST", f"/api/tasks/{task_id}/move", {"to_status": "done"})
    print(f"{task_id} -> done")


def dispatch(task_id: str, comment: str | None = None) -> None:
    if comment:
        req("POST", f"/api/tasks/{task_id}/comment", {"text": comment})
    req("POST", f"/api/tasks/{task_id}/move", {"to_status": "approved"})
    print(f"{task_id} -> approved")


def monitor(task_id: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            status = req("GET", f"/api/tasks/{task_id}").get("status")
        except Exception:
            status = "?error?"
        print(f"[{int(time.time()-t0)}s] {task_id} status: {status}")
        if status in SETTLED:
            print("monitor exit settled")
            return True
        time.sleep(POLL_S)
    print("monitor exit timeout")
    return False


def launcher_tail(n: int = 3) -> None:
    try:
        with open(LOG) as f:
            lines = f.read().splitlines()
        print("launcher:", "\n".join(lines[-n:]))
    except OSError as e:
        print(f"launcher: cannot read log: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--close", help="task id to verify-close (move to done)")
    ap.add_argument("--close-comment", help="verification summary comment on --close")
    ap.add_argument("--next", help="task id to dispatch (move to approved)")
    ap.add_argument("--next-comment", help="comment on --next before dispatch")
    ap.add_argument("--watch", help="task id to monitor until settled")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--progress", help="task id: churn check (exit 0 healthy, 1 churn)")
    ap.add_argument("--prev-bytes", type=int, default=None,
                    help="agent-log bytes at window start (stall detection)")
    args = ap.parse_args()

    if args.progress:
        sys.exit(progress(args.progress, args.prev_bytes))

    if args.close:
        close_task(args.close, args.close_comment)
    if args.next:
        dispatch(args.next, args.next_comment)

    if args.watch:
        time.sleep(3)
        launcher_tail()
        ok = monitor(args.watch, args.timeout)
        launcher_tail()
        sys.exit(0 if ok else 1)
    elif args.close or args.next:
        launcher_tail()
    else:
        ap.error("nothing to do: pass --close/--next/--watch")


if __name__ == "__main__":
    main()

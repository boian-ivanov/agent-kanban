#!/usr/bin/env python3
"""Mid-checkpoint monitor for a kanban task.

Polls the board, the launcher log, the per-task agent log, and the repo
worktree at a fixed interval and reports a checkpoint line each cycle. Flags
stall conditions instead of silently waiting:

  * process alive but agent log not growing AND worktree unchanged
  * agent log / launcher log at 0 bytes since spawn

Usage:
  monitor_checkpoint.py --task T-232 --interval 90 --stall 360 \
      --worktree ~/Projects/salon-platform

Checks each cycle:
  - task status (approved / in_progress / testing / ...)
  - launcher dispatch line + "run finished" for the task
  - per-task agent log byte count + last line
  - live omp process count for --model runs
  - worktree git-change count (files modified/untracked since a baseline)

Exits 0 when status leaves in_progress. Prints one checkpoint per cycle.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%SZ")


def sh(cmd: list[str], cwd: str | None = None) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=20)
        return r.stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"<err {e}>"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--board", default="http://127.0.0.1:7777")
    ap.add_argument("--interval", type=int, default=90)
    ap.add_argument("--stall", type=int, default=360, help="seconds of zero progress before flagging stall")
    ap.add_argument("--worktree", default="")
    ap.add_argument("--launcher-log", default=os.path.expanduser("~/Library/Logs/agent-kanban/launcher.log"))
    ap.add_argument("--agent-log-dir", default=os.path.expanduser("~/Projects/agent-kanban/kanban_data/agent-logs"))
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    agent_log = os.path.join(args.agent_log_dir, f"{args.task}.log")

    # Baseline the worktree change set once.
    baseline: set[str] = set()
    if args.worktree:
        out = sh(["git", "status", "--porcelain"], cwd=args.worktree)
        baseline = {ln.split(maxsplit=1)[0] for ln in out.splitlines() if ln.strip()}

    # Was there a launcher dispatch BEFORE we started monitoring? If not, the
    # task may never have been dispatched.
    launcher_before = sh(["bash", "-lc", f"grep -c '{args.task}' '{args.launcher_log}' || true"])

    last_bytes = -1
    last_progress_ts = time.time()
    start = time.time()
    pending_stall_reported = False
    last_status = None

    def cycle():
        nonlocal last_bytes, last_progress_ts, pending_stall_reported, last_status
        status = sh(["curl", "-s", f"{args.board}/api/tasks/{args.task}"]).strip()
        # pull status via json
        try:
            import json as _json
            status_json = _json.loads(status) if status else {}
            status = status_json.get("status", "?")
        except Exception:  # noqa: BLE001
            status = "?"

        # launcher dispatch + finished lines for this task
        dispatch = sh(["bash", "-lc", f"grep '{args.task}' '{args.launcher_log}' | tail -1 || true"])
        finished = sh(["bash", "-lc", f"grep '{args.task}' '{args.launcher_log}' | grep -c 'run finished' || true"])
        dispatched_cnt = sh(["bash", "-lc", f"grep -c 'omp dispatch for {args.task}' '{args.launcher_log}' || true"])

        # agent log
        log_bytes = 0
        log_last = ""
        if os.path.exists(agent_log):
            log_bytes = os.path.getsize(agent_log)
            log_last = sh(["tail", "-2", agent_log]).replace("\n", " / ")[:160]

        # live omp worker processes
        procs = sh(["bash", "-lc", "ps -axo pid,etimes,args | grep -E '\\.bun/bin/omp' | grep -v grep"]).splitlines()
        # a live agent for this run = an omp --mode json --model process
        live_workers = [p for p in procs if "--mode json" in p]

        # worktree activity since baseline: count files touched in the last
        # 5 minutes (mtime-based) — catches ongoing writes while the agent
        # works, unlike a static set diff which stops changing once files exist.
        changed = 0
        if args.worktree:
            out = sh(["bash", "-lc",
                      f"find '{args.worktree}'/src '{args.worktree}'/tests '{args.worktree}'/package.json "
                      f"'{args.worktree}'/bun.lock -type f -mmin -5 2>/dev/null | wc -l"])
            try:
                changed = int(out.strip() or "0")
            except ValueError:
                changed = 0

        # NOTE: the agent log only captures text_delta (assistant's generated
        # text). Tool calls (reading/writing files, running commands) produce
        # no text_delta, so a *working* agent can show a flat log. The real
        # progress signals are (a) worktree change count and (b) the board
        # status. Use those, not log bytes, to decide stall vs. working.

        grew = log_bytes != last_bytes
        if grew or changed > 0:
            last_progress_ts = time.time()
            pending_stall_reported = False
        last_bytes = log_bytes

        el = int(time.time() - start)
        quiet_secs = int(time.time() - last_progress_ts)

        marks = []
        if status == "testing" or status == "done":
            marks.append("SETTLED")
        if quiet_secs > args.stall and not pending_stall_reported:
            marks.append(f"STALL({quiet_secs}s no progress)")
            pending_stall_reported = True
        if not dispatched_cnt or dispatched_cnt == "0":
            marks.append("NOT_DISPATCHED")

        tag = ",".join(marks) if marks else "ok"
        print(f"[{now()}] CHECKPOINT {args.task} status={status} el={el}s quiet={quiet_secs}s "
              f"log={log_bytes}B grew={grew} worktree_chg={changed} live_workers={len(live_workers)} "
              f"dispatch_cnt={dispatched_cnt} finished_cnt={finished} [{tag}]")
        print(f"   dispatch: {dispatch}")
        if log_last:
            print(f"   log_tail: {log_last}")
        sys.stdout.flush()

    print(f"[{now()}] monitor started for {args.task} (interval={args.interval}s, stall={args.stall}s, "
          f"timeout={args.timeout}s, baseline_worktree_changes={len(baseline)})")
    sys.stdout.flush()

    while True:
        cycle()
        # exit when status leaves in_progress
        # fetch status fresh
        status = "?"
        try:
            import json as _json
            s = sh(["curl", "-s", f"{args.board}/api/tasks/{args.task}"])
            status = _json.loads(s).get("status", "?")
        except Exception:  # noqa: BLE001
            status = "?"
        if status not in ("in_progress", "approved"):  # settled to testing/done/etc
            print(f"[{now()}] monitor done: {args.task} left in_progress -> {status}")
            return 0
        if time.time() - start > args.timeout:
            print(f"[{now()}] monitor TIMEOUT after {args.timeout}s; task still {status}")
            return 2
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
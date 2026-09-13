#!/usr/bin/env python3
"""Orchestrator loop for agent-kanban (user-side verification + dispatch).

Based on the working pattern from 2026-08-16 (T-060 close / T-061 dispatch).

Flow:
  1. (optional) Verify-close a finished task: gate, commit, land the work on
     the base branch, then comment (with the commit hash) + move it to `done`.
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

Lane mode (parallel cards, one worktree each)
---------------------------------------------
A project may opt into one git worktree + branch ``task/<task_id>`` per card
("lane"), so two agents never share a working tree — see task-driver.py, which
allocates the lane at dispatch. The close step follows the work there:

  gate (in the lane) -> commit (in the lane) -> git merge --no-ff task/<id>
  (project tree) -> git push origin <base_branch> -> examples/lane-release.sh
  <project_id> <task_id> -> comment the hash + move the card to `done`

Lane config is resolved in this order, first hit wins:
  1. ``LANE_PROJECT_PATH`` — test hook: the project tree, with no board call,
  2. ``GET /api/board?project=<id>`` -> ``.project.path`` + ``.project.worktrees``,
  3. ``kanban_data/worktrees.json`` keyed by project id.

No config (or no worktree on ``task/<id>``, which warns) => the shared tree
``project.path`` is used and no ``git worktree`` command is issued: exactly the
pre-lane close. The merge point stays serial on purpose — a conflict prints
the unmerged files, exits non-zero and does NOT release the lane, so the work
is still on disk for a human to resolve.

``LANE_CONFIG`` (JSON config file) is the matching test hook, shared with
``lane-release.sh`` so one environment drives both.

Settled states: testing, uat, done, blocked, cancelled.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

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
DEFAULT_GATE = "bun check"
REPO_ROOT = Path(__file__).resolve().parent.parent
WORKTREES_JSON = REPO_ROOT / "kanban_data" / "worktrees.json"
LANE_RELEASE = Path(__file__).resolve().parent / "lane-release.sh"


class CloseError(RuntimeError):
    """The close step refused to continue — nothing beyond it has run."""


@dataclass
class Lane:
    """Where a card's close step runs (see the module docstring).

    ``lane`` is None in shared-tree mode. ``wanted`` records that lane config
    was present but no worktree carried this card's branch: that fallback is
    the one case the operator must see instead of guessing from the paths.
    """

    project_id: str
    repo: Path
    branch: str
    base_branch: str
    lane: Path | None = None
    wanted: bool = False

    @property
    def tree(self) -> Path:
        """The tree that holds the work: the lane, else the project tree."""
        return self.lane or self.repo

    @property
    def mode(self) -> str:
        if self.lane:
            return "lane"
        return "shared (lane config, no worktree)" if self.wanted else "shared"


def lane_branch(task_id: str) -> str:
    """Lane identity: ``task/<task_id>`` — the same string task-driver.py uses."""
    return f"task/{task_id}"


def git(repo: Path, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise CloseError(
            f"git {' '.join(argv)} failed in {repo} ({proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc


def board_project(project_id: str) -> dict:
    """`.project` from the board, or {} when the board cannot answer.

    Lane discovery sits on top of an on-disk config, so a board hiccup must
    not abort a close: warn and let the file (or the shared tree) decide.
    """
    try:
        body = req("GET", f"/api/board?project={urllib.parse.quote(project_id)}")
    except Exception as e:  # noqa: BLE001 — any transport error means "no answer"
        print(f"warn: board lookup failed for {project_id!r}: {e}")
        return {}
    project = (body or {}).get("project")
    return project if isinstance(project, dict) else {}


def file_lane_config(project_id: str) -> dict | None:
    """`worktrees.json` entry for a project, or None (lane mode off).

    Same contract as task-driver.py: missing or corrupt file, or
    ``enabled: false``, means the shared tree. That is the safe direction —
    a broken config must never silently split the project tree.
    """
    path = Path(os.environ.get("LANE_CONFIG") or WORKTREES_JSON)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"warn: unreadable {path}: {e} — lane mode off")
        return None
    cfg = data.get(project_id) if isinstance(data, dict) else None
    if not isinstance(cfg, dict) or not cfg.get("enabled", True):
        return None
    return cfg


def worktree_for_branch(repo: Path, branch: str) -> Path | None:
    """Path of the worktree checked out on `branch`, or None.

    ``git worktree list --porcelain`` is the only ledger (no registry file),
    from the project tree — the same probe task-driver.py uses to resume.
    """
    proc = git(repo, "worktree", "list", "--porcelain", check=False)
    if proc.returncode != 0:
        print(f"warn: git worktree list failed ({proc.returncode}): {proc.stderr.strip()}")
        return None
    path: Path | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
        elif line.startswith("branch refs/heads/"):
            if line[len("branch refs/heads/") :] == branch and path and path.exists():
                return path
    return None


def resolve_lane(task_id: str, project_id: str) -> Lane:
    """Resolve the tree holding this card's work (module docstring, "Lane mode").

    Raises CloseError when no project path is discoverable at all: committing
    into a guessed directory is worse than stopping.
    """
    override = os.environ.get("LANE_PROJECT_PATH")
    board = {} if override else board_project(project_id)
    repo = Path(override).expanduser() if override else board.get("path")
    if not repo:
        raise CloseError(
            f"no project path for {project_id!r}: the board has none and "
            "LANE_PROJECT_PATH is unset"
        )

    board_cfg = board.get("worktrees")
    if isinstance(board_cfg, dict):
        # The board field wins once a project uses it; `enabled: false` there
        # is a deliberate off switch, so do not fall through to the file.
        cfg = board_cfg if board_cfg.get("enabled", True) else None
    else:
        cfg = file_lane_config(project_id)

    branch = lane_branch(task_id)
    lane = worktree_for_branch(repo, branch) if cfg else None
    if cfg and lane is None:
        print(
            f"warn: lane mode is on for {project_id} but no worktree is on "
            f"branch {branch} — falling back to the shared tree {repo}"
        )
    return Lane(
        project_id=project_id,
        repo=repo,
        branch=branch,
        base_branch=str((cfg or {}).get("base_branch") or "master"),
        lane=lane,
        wanted=bool(cfg),
    )


def run_gate(repo: Path, gate: str) -> None:
    """Run the repo gate in the tree whose work is about to be committed.

    Exit status is read directly: `bun check | tail` reports tail's status,
    so a piped gate can look green while red (SP-007 session).
    """
    if not gate:
        print("[gate] none (--gate '')")
        return
    print(f"[gate] {gate}  (cwd {repo})")
    rc = subprocess.run(gate, shell=True, cwd=str(repo), check=False).returncode
    print(f"[gate] exit {rc}")
    if rc != 0:
        raise CloseError(f"gate failed (exit {rc}): {gate}")


def commit_all(repo: Path, message: str) -> str | None:
    """Commit everything in `repo`; None when the tree is already clean.

    `git add -A` (not `-u`) on purpose: a card's work includes new files, and
    the tree is card-owned (a lane, or the shared tree in no-lane mode).
    """
    if not git(repo, "status", "--porcelain").stdout.strip():
        print(f"[commit] nothing to commit in {repo}")
        return None
    git(repo, "add", "-A")
    proc = git(repo, "commit", "-m", message, check=False)
    if proc.returncode != 0:
        # A failing pre-commit hook means the gate said no — never --no-verify.
        raise CloseError(
            f"git commit failed in {repo} ({proc.returncode}): "
            f"{(proc.stdout + proc.stderr).strip()}"
        )
    print("\n".join(proc.stdout.splitlines()[:1]))
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def merge_lane(lane: Lane) -> str:
    """`git merge --no-ff task/<id>` in the project tree; returns the merge sha.

    The base branch must be what the project tree has checked out: the merge
    lands on HEAD, so pushing ``base_branch`` while HEAD is something else
    would publish a stale ref.
    """
    head = git(lane.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if head != lane.base_branch:
        raise CloseError(
            f"{lane.repo} has {head!r} checked out, expected {lane.base_branch!r} "
            "— refusing to merge"
        )
    proc = git(lane.repo, "merge", "--no-ff", lane.branch, check=False)
    print(f"[merge] {(proc.stdout + proc.stderr).strip()}")
    if proc.returncode != 0:
        conflicts = git(lane.repo, "diff", "--name-only", "--diff-filter=U").stdout.split()
        listing = "\n".join(f"  {f}" for f in conflicts) or "  (no unmerged paths listed)"
        # The merge is left in place on purpose: the lane still exists, so the
        # work survives for the human who resolves it here.
        raise CloseError(
            f"merge of {lane.branch} into {lane.base_branch} failed — resolve in "
            f"{lane.repo} (`git merge --abort` to back out), then re-run.\n{listing}"
        )
    return git(lane.repo, "rev-parse", "HEAD").stdout.strip()


def push_base(lane: Lane) -> None:
    proc = git(lane.repo, "push", "origin", lane.base_branch, check=False)
    print(f"[push] {(proc.stdout + proc.stderr).strip()}")
    if proc.returncode != 0:
        raise CloseError(f"git push origin {lane.base_branch} failed ({proc.returncode})")


def release_lane(project_id: str, task_id: str) -> None:
    """Hand the lane back via examples/lane-release.sh (removal lives there).

    Keeping release a separate shell entry point leaves the merge point one
    inspectable command; a failed release does not undo a pushed merge, it
    only leaves the lane behind for `lane-release.sh --check`.
    """
    script = LANE_RELEASE
    if not script.exists():
        raise CloseError(f"{script} not found — cannot release the lane")
    proc = subprocess.run([str(script), project_id, task_id], check=False)
    if proc.returncode != 0:
        raise CloseError(f"{script.name} failed ({proc.returncode}) for {task_id}")


def close_lane(
    task_id: str,
    project_id: str,
    *,
    gate: str = DEFAULT_GATE,
    message: str | None = None,
    release: bool = True,
    dry_run: bool = False,
) -> str | None:
    """gate -> commit -> merge -> push -> release; returns the landed commit.

    The hash is returned (not printed only) so the close comment can carry
    the commit that is actually on the base branch.
    """
    lane = resolve_lane(task_id, project_id)
    print(f"[lane] {lane.project_id} mode={lane.mode} worktree={lane.tree}")
    if dry_run:
        steps = [f"gate `{gate or 'none'}` in {lane.tree}", f"commit in {lane.tree}"]
        if lane.lane:
            steps += [
                f"merge --no-ff {lane.branch} in {lane.repo}",
                f"push origin {lane.base_branch}",
            ]
            if release:
                steps.append(f"release {project_id} {task_id}")
        else:
            steps.append(f"push origin {lane.base_branch}")
        print("[dry-run] would: " + "; ".join(steps))
        return None

    run_gate(lane.tree, gate)
    sha = commit_all(lane.tree, message or f"chore({task_id}): verified card")
    if lane.lane:
        sha = merge_lane(lane)  # the merge commit is what lands on the base
        push_base(lane)
        if release:
            release_lane(project_id, task_id)
    else:
        push_base(lane)
    return sha


def task_info(task_id: str) -> dict:
    """Task row from the board, or {} — the close step also runs offline."""
    try:
        return req("GET", f"/api/tasks/{task_id}") or {}
    except Exception as e:  # noqa: BLE001 — best-effort metadata only
        print(f"warn: no task payload for {task_id}: {e}")
        return {}


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
    ap.add_argument("--gate", default=os.environ.get("KANBAN_GATE") or DEFAULT_GATE,
                    help=f"gate run before the commit, in the card's tree "
                         f"(default: {DEFAULT_GATE}; empty string skips it)")
    ap.add_argument("--commit-message",
                    help="commit message (default: chore(<task id>): <task title>)")
    ap.add_argument("--project",
                    help="project id for lane lookup (default: the task's project)")
    ap.add_argument("--no-release", action="store_true",
                    help="keep the card's lane after merging (skip lane-release.sh)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved lane and the planned commands, then "
                         "exit without touching git or the board")
    args = ap.parse_args()

    if args.progress:
        sys.exit(progress(args.progress, args.prev_bytes))

    if args.close:
        task = task_info(args.close)
        project_id = args.project or task.get("project_id")
        if not project_id:
            sys.exit(f"--close {args.close}: no project id — pass --project")
        message = args.commit_message or f"chore({args.close}): {task.get('title') or 'verified card'}"
        try:
            sha = close_lane(
                args.close,
                project_id,
                gate=args.gate,
                message=message,
                release=not args.no_release,
                dry_run=args.dry_run,
            )
        except CloseError as e:
            sys.exit(f"close failed: {e}")
        if args.dry_run:
            sys.exit(0)
        comment = args.close_comment
        if sha:
            comment = f"{comment}\n\nCommit: {sha}" if comment else f"Commit: {sha}"
        close_task(args.close, comment)
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

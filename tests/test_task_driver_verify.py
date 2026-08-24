"""T-314: verification-agent driver (--mode verify).

Runs examples/task-driver.py --mode verify against a fake kanban API and a
fake omp binary (OMP_BIN). Covers:
  - PASS: verifier comments evidence, task moves to done, run registered
    with role=verification, no claim posted (task stays in testing while
    the verifier works);
  - FAIL: verifier comments exact findings, task moves to approved;
  - guard: a task with a live implementer run (status=running, live pid)
    is NOT dispatched — the driver waits, then skips with a comment;
  - a dead-pid run row does not block dispatch;
  - abort when the task is not in testing.

The fake omp binary plays the verifier agent: after the initial prompt it
POSTs the verdict comment and the move itself, exactly like the real agent
would over curl.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER = REPO_ROOT / "examples" / "task-driver.py"

FAKE_OMP = """#!/usr/bin/env python3
import json, os, signal, sys, time

mode = os.environ.get("FAKE_MODE", "silent")
base = os.environ.get("FAKE_BASE_URL", "")
task_id = os.environ.get("FAKE_TASK_ID", "T-V")

signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def post(path, obj):
    import urllib.request
    req = urllib.request.Request(
        base + path,
        data=json.dumps(obj).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=5).read()

emit({"type": "ready", "protocolVersion": 1})
time.sleep(0.2)
while True:
    line = sys.stdin.readline()
    if not line:
        time.sleep(1)
        continue
    try:
        obj = json.loads(line)
    except ValueError:
        continue
    if obj.get("type") == "prompt":
        break

if mode == "verify_done":
    post(f"/api/tasks/{task_id}/comment",
         {"text": "PASS: repo gate green (bun check exit 0, pytest 42 passed)"})
    post(f"/api/tasks/{task_id}/move", {"to_status": "done"})
    emit({"type": "message_update",
          "assistantMessageEvent": {"type": "text_delta", "delta": "verdict "}})
    emit({"type": "message_end", "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "PASS: repo gate green"}],
        "usage": {"input": 10, "output": 5, "totalTokens": 15},
    }})
    emit({"type": "agent_end", "messages": [], "isTerminal": True})
elif mode == "verify_fail":
    post(f"/api/tasks/{task_id}/comment",
         {"text": "FAIL: bun check exit 1 — src/App.svelte:12 type error"})
    post(f"/api/tasks/{task_id}/move", {"to_status": "approved"})
    emit({"type": "agent_end", "messages": [], "isTerminal": True})
else:  # silent — never finishes (guard timeout path uses this)
    while True:
        time.sleep(1)

# wait for the driver to close stdin, then exit
while sys.stdin.readline():
    pass
sys.exit(0)
"""


class FakeBoard:
    """Minimal kanban API for the driver's verify flow; records everything
    the driver (and fake omp agent) POSTs."""

    def __init__(self) -> None:
        self.task_status = "testing"
        self.assignee = "agent:be"  # implementer still owns the assignee
        self.run: dict = {"status": "done", "pid": None}
        self.comments: list[str] = []
        self.moves: list[str] = []
        self.claims: list[str] = []
        self.run_posts: list[dict] = []

    def serve(self) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._server = server
        return f"http://127.0.0.1:{server.server_port}"

    def close(self) -> None:
        self._server.shutdown()


def _handler(board: FakeBoard):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence test output
            pass

        def _task(self) -> dict:
            return {
                "id": "T-V",
                "title": "verify test",
                "status": board.task_status,
                "assignee": board.assignee,
                "kind": "task",
                "parent_id": None,
                "ancestors": [],
                "history": [],
            }

        def _context(self) -> dict:
            return {
                "task_id": "T-V",
                "project_id": "agent-kanban",
                "task": self._task(),
                "ancestors": [],
                "comments": [],
                "constraints": ["constraint one"],
            }

        def _read(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else {}

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path.startswith("/api/board"):
                self._send(200, {"project": {}})
            elif "since_seq" in self.path:
                self._send(200, {"history": []})
            elif self.path.endswith("/context"):
                self._send(200, self._context())
            elif self.path.endswith("/runs"):
                self._send(200, {"run": board.run})
            elif "/api/tasks/" in self.path:
                self._send(200, self._task())
            else:
                self._send(404, {"detail": "no"})

        def do_POST(self) -> None:
            body = self._read()
            if "/claim" in self.path:
                board.claims.append(body.get("assignee", ""))
                self._send(200, self._task())
            elif "/comment" in self.path:
                board.comments.append(body.get("text", ""))
                self._send(200, {"ok": True})
            elif "/move" in self.path:
                to_status = body.get("to_status", "")
                board.moves.append(to_status)
                board.task_status = to_status or board.task_status
                self._send(200, self._task())
            elif "/runs" in self.path:
                board.run_posts.append(body)
                board.run = {**board.run, **{k: v for k, v in body.items() if v}}
                self._send(200, {"run": board.run})
            elif "/chat" in self.path:
                self._send(201, {"seq": 1})
            else:
                self._send(404, {"detail": "no"})

    return H


def run_driver(
    board_url: str,
    task_id: str,
    tmp_path: Path,
    *extra_args: str,
    mode: str = "verify_done",
) -> subprocess.CompletedProcess:
    fake = tmp_path / "fake_omp.py"
    fake.write_text(FAKE_OMP)
    fake.chmod(0o755)
    env = {
        **os.environ,
        "OMP_BIN": str(fake),
        "FAKE_MODE": mode,
        "FAKE_BASE_URL": board_url,
        "FAKE_TASK_ID": task_id,
    }
    cmd = [
        sys.executable,
        str(DRIVER),
        "--task-id",
        task_id,
        "--project-id",
        "agent-kanban",
        "--base-url",
        board_url,
        "--mode",
        "verify",
        *extra_args,
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
        cwd=REPO_ROOT,
        check=False,
    )


@pytest.fixture()
def board():
    b = FakeBoard()
    url = b.serve()
    yield b, url
    b.close()


def test_verify_pass_comments_evidence_and_moves_done(board, tmp_path):
    """Green task in testing -> verifier runs, comments PASS evidence,
    moves done; no claim (assignee stays with the implementer)."""
    b, url = board
    proc = run_driver(url, "T-VP", tmp_path, mode="verify_done")

    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    assert any("PASS" in c and "gate green" in c for c in b.comments), b.comments
    assert b.moves == ["done"], b.moves
    assert b.claims == [], f"verifier must not claim: {b.claims}"
    # run registered with the verification role, then marked done
    roles = [p.get("role") for p in b.run_posts if p.get("status") == "running"]
    assert roles == ["verification"], b.run_posts
    assert b.run_posts[-1].get("status") == "done", b.run_posts


def test_verify_fail_comments_findings_and_moves_approved(board, tmp_path):
    """Broken change -> verifier comments exact failure and moves approved
    (auto-retriggers the fix run)."""
    b, url = board
    proc = run_driver(url, "T-VF", tmp_path, mode="verify_fail")

    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    assert any(
        "FAIL" in c and "bun check exit 1" in c for c in b.comments
    ), b.comments
    assert b.moves == ["approved"], b.moves


def test_verify_guard_skips_when_run_still_live(board, tmp_path):
    """Guard: dispatch is skipped while a live agent process still owns the
    task (run status=running with a live pid)."""
    b, url = board
    b.run = {"status": "running", "pid": os.getpid()}  # live implementer
    proc = run_driver(url, "T-VG", tmp_path, "--guard-timeout", "2", mode="silent")

    assert proc.returncode == 3, f"expected guard skip:\n{proc.stdout}\n{proc.stderr}"
    assert any("Verification skipped" in c for c in b.comments), b.comments
    # no verifier run was ever registered, task was not moved
    assert b.run_posts == [], b.run_posts
    assert b.moves == [], b.moves


def test_verify_guard_passes_when_pid_dead(board, tmp_path):
    """A running-marked run row with a dead pid does not block dispatch."""
    b, url = board
    b.run = {"status": "running", "pid": 2_147_483_647}  # impossible pid
    proc = run_driver(url, "T-VD", tmp_path, mode="verify_done")

    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    assert b.moves == ["done"], b.moves


def test_verify_aborts_when_task_not_in_testing(board, tmp_path):
    b, url = board
    b.task_status = "approved"  # stale dispatch — implementer re-claimed
    proc = run_driver(url, "T-VA", tmp_path, mode="verify_done")

    assert proc.returncode == 3, f"expected abort:\n{proc.stdout}\n{proc.stderr}"
    assert b.moves == [], b.moves
    assert b.run_posts == [], b.run_posts

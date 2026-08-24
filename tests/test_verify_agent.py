"""T-314 verification agent: driver --mode verify end-to-end.

Runs examples/task-driver.py --mode verify as a subprocess against a fake
kanban API and a fake omp binary (OMP_BIN), covering the three behaviors:

- PASS: verifier runs the gate, moves the task to done -> driver exits 0,
  run row role=verification, no claim was made (task stayed in testing);
- FAIL: verifier moves the task to approved (auto-retrigger) -> same driver
  completion, last move is approved;
- guard: a live agent run (status=running + alive pid) blocks dispatch for
  --guard-timeout, then the driver skips with a comment and exits 3; when
  the live run clears, dispatch proceeds.

Acceptance (T-314): "Green task in testing -> verifier runs, comments PASS
evidence, moves done; broken change -> verifier comments exact failure and
moves approved."
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
AGENT_LOG_DIR = REPO_ROOT / "kanban_data" / "agent-logs"

FAKE_OMP = """#!/usr/bin/env python3
import json, os, signal, sys, time, urllib.request

mode = os.environ.get("FAKE_MODE", "pass")
base = os.environ["FAKE_BASE_URL"]
task_id = os.environ["FAKE_TASK_ID"]
prompt_file = os.environ.get("FAKE_PROMPT_FILE")

signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def move(to_status):
    req = urllib.request.Request(
        base + "/api/tasks/" + task_id + "/move",
        data=json.dumps({"to_status": to_status}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10).read()

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

if prompt_file:
    with open(prompt_file, "w") as f:
        f.write(obj.get("message", ""))

# the verifier agent's own verdict move
if mode == "pass":
    move("done")
elif mode == "fail":
    move("approved")

emit({"type": "message_end", "message": {
    "role": "assistant",
    "content": [{"type": "text", "text": "verdict evidence"}],
}})
emit({"type": "agent_end", "messages": [], "isTerminal": True})
while sys.stdin.readline():
    pass
sys.exit(0)
"""


class FakeBoard:
    """Minimal kanban API: the endpoints task-driver.py drives in verify
    mode. ``live_runs`` controls the /runs GET response for guard tests:
    a list of (status, pid) served in order, last one repeating."""

    def __init__(self) -> None:
        self.task_status = "testing"
        self.claims: list[str] = []
        self.moves: list[str] = []
        self.comments: list[str] = []
        self.run_posts: list[dict] = []
        self.live_runs: list[tuple[str, int | None]] = []
        self._runs_gets = 0

    def serve(self) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._server = server
        return f"http://127.0.0.1:{server.server_port}"

    def close(self) -> None:
        self._server.shutdown()

    def run_row(self) -> dict:
        if not self.live_runs:
            return {}
        status, pid = self.live_runs[min(self._runs_gets, len(self.live_runs) - 1)]
        return {"status": status, "pid": pid}


def _handler(board: FakeBoard):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence test output
            pass

        def _task(self) -> dict:
            return {
                "id": "T-VER",
                "title": "verify test",
                "status": board.task_status,
                "assignee": "agent:default",
                "kind": "task",
                "parent_id": None,
                "ancestors": [],
                "history": [],
            }

        def _context(self) -> dict:
            return {
                "task_id": "T-VER",
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
                board._runs_gets += 1
                self._send(200, {"run": board.run_row()})
            elif "/api/tasks/" in self.path:
                self._send(200, self._task())
            else:
                self._send(404, {"detail": "no"})

        def do_POST(self) -> None:
            if "/claim" in self.path:
                board.claims.append(self._read().get("assignee", ""))
                self._send(200, self._task())
            elif "/comment" in self.path:
                board.comments.append(self._read().get("text", ""))
                self._send(200, {"ok": True})
            elif "/move" in self.path:
                to_status = self._read().get("to_status", "")
                board.moves.append(to_status)
                board.task_status = to_status or board.task_status
                self._send(200, self._task())
            elif "/runs" in self.path:
                board.run_posts.append(self._read())
                self._send(200, {"run": {}})
            elif "/chat" in self.path:
                self._read()
                self._send(201, {"seq": 1})
            else:
                self._send(404, {"detail": "no"})

    return H


def run_driver(
    board_url: str,
    tmp_path: Path,
    mode: str = "pass",
    task_id: str = "T-VER",
    guard_args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess, Path]:
    fake = tmp_path / "fake_omp.py"
    fake.write_text(FAKE_OMP)
    fake.chmod(0o755)
    prompt_file = tmp_path / "prompt.txt"
    env = {
        **os.environ,
        "OMP_BIN": str(fake),
        "FAKE_MODE": mode,
        "FAKE_BASE_URL": board_url,
        "FAKE_TASK_ID": task_id,
        "FAKE_PROMPT_FILE": str(prompt_file),
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
        *(guard_args or []),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
        cwd=REPO_ROOT,
        check=False,
    )
    return proc, prompt_file


@pytest.fixture()
def board():
    b = FakeBoard()
    url = b.serve()
    yield b, url
    b.close()


def _clean_log(task_id: str) -> None:
    log = AGENT_LOG_DIR / f"{task_id}.log"
    log.unlink(missing_ok=True)


def test_verify_pass_moves_done_no_claim(board, tmp_path):
    """PASS: verifier comments evidence and moves done; no claim (task stays
    in testing), run row role=verification, driver exits 0."""
    b, url = board
    proc, prompt_file = run_driver(url, tmp_path, mode="pass")

    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    # verify mode never claims: the task stays assigned to the implementer
    assert b.claims == [], f"verify mode must not claim, got: {b.claims}"
    assert b.moves == ["done"], f"moves: {b.moves}"
    assert b.task_status == "done"
    # run row: registered as verification, finished done
    assert b.run_posts, "driver never registered a run row"
    assert b.run_posts[0].get("role") == "verification"
    assert b.run_posts[0].get("status") == "running"
    assert b.run_posts[-1].get("status") == "done"
    # prompt carries the verification protocol + repo gate + D1 no-commit
    prompt = prompt_file.read_text(encoding="utf-8")
    assert "Board protocol (verification)" in prompt
    assert "bun run format" in prompt and "bun check" in prompt
    assert "NEVER commit" in prompt
    assert "FAIL -> approved" in prompt
    _clean_log("T-VER")


def test_verify_fail_moves_approved(board, tmp_path):
    """FAIL: verifier comments exact findings and moves approved (the fix
    rule auto-retriggers from there)."""
    b, url = board
    proc, _ = run_driver(url, tmp_path, mode="fail")

    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    assert b.moves == ["approved"], f"moves: {b.moves}"
    assert b.task_status == "approved"
    assert b.run_posts[-1].get("status") == "done"
    _clean_log("T-VER")


def test_verify_aborts_when_task_not_in_testing(board, tmp_path):
    """Guard: a task that is not in testing is never verified (stale event /
    manual invocation) — driver exits 3 without registering a run."""
    b, url = board
    b.task_status = "approved"
    proc, _ = run_driver(url, tmp_path, mode="pass")

    assert proc.returncode == 3, f"expected exit 3, got {proc.returncode}"
    assert b.run_posts == [], "no run row for an aborted verify"
    assert b.moves == []
    _clean_log("T-VER")


def test_verify_guard_skips_when_run_stays_live(board, tmp_path):
    """Guard: a live agent process still owning the task blocks dispatch; the
    driver waits --guard-timeout then skips with a comment and exits 3."""
    b, url = board
    b.live_runs = [("running", os.getpid())]  # alive for the whole test
    proc, _ = run_driver(url, tmp_path, mode="pass", guard_args=["--guard-timeout", "1"])

    assert proc.returncode == 3, f"expected guard skip, got {proc.returncode}"
    assert any("Verification skipped" in c for c in b.comments), (
        f"skip comment missing: {b.comments}"
    )
    assert b.run_posts == [], "skipped verify must not register a run"
    assert b.moves == []
    _clean_log("T-VER")


def test_verify_guard_proceeds_once_run_clears(board, tmp_path):
    """Guard: when the implementer's run clears a beat after the move (its
    driver marks the run done), dispatch proceeds normally."""
    b, url = board
    # first /runs GET: implementer still live; afterwards: cleared
    b.live_runs = [("running", os.getpid()), ("done", os.getpid())]
    proc, _ = run_driver(
        url, tmp_path, mode="pass", guard_args=["--guard-timeout", "10"]
    )

    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    assert b.moves == ["done"]
    assert b.run_posts and b.run_posts[-1].get("status") == "done"
    _clean_log("T-VER")

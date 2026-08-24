"""T-312 token/duration watchdog: driver kills budget-breaching runs.

Runs examples/task-driver.py as a subprocess against a fake kanban API and
a fake omp binary (OMP_BIN). Each breach kind (max_tokens, max_duration,
no_progress, dot_only) is exercised end-to-end: the run is killed, an
evidence comment is posted, the task is moved back to approved, and the omp
child is reaped (no orphans).

Acceptance (T-312): "With max_tokens=10000 on a test task, run killed at
budget with evidence comment and task returns to approved; no orphan omp
processes after kill."
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
import json, os, signal, sys, time

mode = os.environ.get("FAKE_MODE", "silent")
pidfile = os.environ.get("FAKE_PIDFILE")
if pidfile:
    open(pidfile, "w").write(str(os.getpid()))

signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

emit({"type": "ready", "protocolVersion": 1})
time.sleep(0.2)
# wait for the initial prompt command
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

if mode == "tokens":
    emit({"type": "message_update",
          "assistantMessageEvent": {"type": "text_delta", "delta": "working "}})
    emit({"type": "message_end", "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "working"}],
        "usage": {"input": 14000, "output": 1000, "totalTokens": 15000},
    }})
    emit({"type": "agent_end", "messages": [], "isTerminal": True})
elif mode == "dots":
    emit({"type": "message_update",
          "assistantMessageEvent": {"type": "text_delta", "delta": "." * 2000}})
    emit({"type": "agent_end", "messages": [], "isTerminal": True})
# silent: no output at all — the driver must kill us on budget breach

while True:
    time.sleep(1)
"""


class FakeBoard:
    """Minimal kanban API: the endpoints task-driver.py drives, recording
    comments and status moves."""

    def __init__(self) -> None:
        self.comments: list[str] = []
        self.moves: list[str] = []
        self.task_status = "in_progress"

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
                "id": "T-WD",
                "title": "watchdog test",
                "status": board.task_status,
                "assignee": "agent:default",
                "history": [],
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
            elif "/api/tasks/" in self.path:
                self._send(200, self._task())
            else:
                self._send(404, {"detail": "no"})

        def do_POST(self) -> None:
            if "/claim" in self.path:
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
                self._read()
                self._send(200, {"run": {}})
            elif "/chat" in self.path:
                self._read()
                self._send(201, {"seq": 1})
            else:
                self._send(404, {"detail": "no"})

    return H


def run_driver(
    board_url: str,
    task_id: str,
    tmp_path: Path,
    *extra_args: str,
    mode: str = "silent",
) -> tuple[subprocess.CompletedProcess, Path]:
    fake = tmp_path / "fake_omp.py"
    fake.write_text(FAKE_OMP)
    fake.chmod(0o755)
    pidfile = tmp_path / f"{task_id}.pid"
    env = {
        **os.environ,
        "OMP_BIN": str(fake),
        "FAKE_MODE": mode,
        "FAKE_PIDFILE": str(pidfile),
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
        *extra_args,
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
    return proc, pidfile


def process_alive(pidfile: Path) -> bool:
    if not pidfile.exists():
        return True  # never verified — fail the assertion
    pid = int(pidfile.read_text().strip())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture()
def board():
    b = FakeBoard()
    url = b.serve()
    yield b, url
    b.close()


@pytest.mark.parametrize(
    ("task_id", "mode", "extra", "breach", "comment_marker"),
    [
        (
            "T-WD-TOK",
            "tokens",
            ("--max-tokens", "10000"),
            "max_tokens",
            "tokens=15000/10000",
        ),
        ("T-WD-DUR", "silent", ("--max-duration", "1"), "max_duration", "max_duration"),
        (
            "T-WD-NP",
            "silent",
            ("--watchdog-window", "1", "--watchdog-min-growth", "1"),
            "no_progress",
            "no_progress",
        ),
        ("T-WD-DOT", "dots", (), "dot_only", "dot_only"),
    ],
)
def test_budget_breach_kills_run_and_returns_to_approved(
    board, tmp_path, task_id, mode, extra, breach, comment_marker
):
    b, url = board
    proc, pidfile = run_driver(url, task_id, tmp_path, *extra, mode=mode)

    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    # evidence comment posted (elapsed, tokens, log bytes like --progress)
    assert any(
        f"Budget breach ({breach})" in c and "[progress]" in c and "log=" in c
        for c in b.comments
    ), f"comments: {b.comments}"
    if comment_marker:
        assert any(comment_marker in c for c in b.comments), f"comments: {b.comments}"
    # task returns to approved
    assert b.moves[-1] == "approved", f"moves: {b.moves}"
    # no orphan omp process after the kill
    assert not process_alive(pidfile), f"omp child {pidfile} still alive"
    # agent log records the breach
    log = AGENT_LOG_DIR / f"{task_id}.log"
    assert log.exists() and "budget breach" in log.read_text(encoding="utf-8")
    log.unlink(missing_ok=True)

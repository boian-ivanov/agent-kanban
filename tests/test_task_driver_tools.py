"""The agent log must show what the agent is DOING, not only what it says.

Why: a tool-heavy run emits almost no assistant text, so the board's Agent
output panel read as empty while the agent was working (SP-049: 310 lines,
249 blank). The driver now writes one compact line per tool call from the RPC
`tool_execution_start` / `tool_execution_end` events, which is the only
progress signal available during long tool stretches.
"""

from __future__ import annotations

import importlib.util
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


def load_driver():
    spec = importlib.util.spec_from_file_location("task_driver_tools_under_test", DRIVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driver = load_driver()


# ---------------------------------------------------------------------------
# Unit: the line shape
# ---------------------------------------------------------------------------


def test_tool_line_uses_the_models_intent():
    line = driver._tool_line(
        {"toolName": "read", "args": {"path": "sample.txt"}, "intent": "Reading sample.txt"}
    )
    assert line == "read Reading sample.txt"


def test_tool_line_falls_back_to_arguments_and_truncates():
    line = driver._tool_line({"toolName": "bash", "args": {"command": "echo hi"}})
    assert line == 'bash {"command": "echo hi"}'

    long = driver._tool_line({"toolName": "grep", "intent": "x" * 400})
    assert long.endswith("…")
    assert len(long) <= len("grep ") + 160, "intent is capped, never unbounded"
    assert "x" * 400 not in long


def test_tool_line_survives_a_missing_name():
    assert driver._tool_line({}).startswith("tool")


# ---------------------------------------------------------------------------
# End to end: the real driver against a fake board + fake omp
# ---------------------------------------------------------------------------

FAKE_OMP = """#!/usr/bin/env python3
import json, sys
def emit(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
emit({"type": "ready", "protocolVersion": 1})
while True:
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    try:
        obj = json.loads(line)
    except ValueError:
        continue
    if obj.get("type") == "prompt":
        break
emit({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "read",
      "args": {"path": "sample.txt"}, "intent": "Reading sample.txt"})
emit({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "read",
      "result": {"content": [{"type": "text", "text": "hello"}]}, "isError": False})
emit({"type": "tool_execution_start", "toolCallId": "c2", "toolName": "bash",
      "args": {"command": "bun check"}, "intent": "Running the gate"})
emit({"type": "tool_execution_end", "toolCallId": "c2", "toolName": "bash",
      "result": {"content": [{"type": "text", "text": "boom"}]}, "isError": True})
emit({"type": "message_update", "assistantMessageEvent": {
    "type": "text_delta", "delta": "gate failed"}})
emit({"type": "message_end", "message": {"role": "assistant",
      "content": [{"type": "text", "text": "gate failed"}], "usage": {"totalTokens": 5}}})
emit({"type": "agent_end", "messages": [], "isTerminal": True})
while sys.stdin.readline():
    pass
"""


class FakeBoard:
    """Only what the driver's work path touches."""

    def __init__(self) -> None:
        self.comments: list[str] = []
        self.run: dict = {}
        self.task_status = "approved"
        self.status_after_register = "testing"

    def serve(self) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._server = server
        return f"http://127.0.0.1:{server.server_port}"

    def close(self) -> None:
        self._server.shutdown()


def _handler(board: FakeBoard):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _task(self) -> dict:
            return {
                "id": "T-TOOL",
                "title": "tool logging",
                "status": board.task_status,
                "assignee": "agent:default",
                "kind": "task",
                "parent_id": None,
                "ancestors": [],
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
                self._send(200, {"project": {"id": "agent-kanban", "path": str(REPO_ROOT)}})
            elif "since_seq" in self.path:
                self._send(200, {"history": []})
            elif self.path.endswith("/context"):
                self._send(
                    200,
                    {"task_id": "T-TOOL", "task": self._task(), "ancestors": [], "comments": []},
                )
            elif self.path.endswith("/runs"):
                self._send(200, {"run": board.run})
            else:
                self._send(200, self._task())

        def do_POST(self) -> None:
            if "/comment" in self.path:
                board.comments.append(self._read().get("text", ""))
                self._send(200, {"ok": True})
            elif "/runs" in self.path:
                body = self._read()
                board.run = {**board.run, **{k: v for k, v in body.items() if v}}
                if body.get("status") == "running":
                    board.task_status = board.status_after_register
                self._send(200, {"run": board.run})
            elif "/move" in self.path:
                board.task_status = self._read().get("to_status") or board.task_status
                self._send(200, self._task())
            elif "/chat" in self.path:
                self._read()
                self._send(201, {"seq": 1})
            else:
                self._send(200, self._task())

    return H


@pytest.fixture()
def board():
    b = FakeBoard()
    url = b.serve()
    yield b, url
    b.close()
    (AGENT_LOG_DIR / "T-TOOL.log").unlink(missing_ok=True)


def test_tool_calls_are_logged_with_their_intent(board, tmp_path):
    b, url = board
    fake = tmp_path / "fake_omp.py"
    fake.write_text(FAKE_OMP)
    fake.chmod(0o755)
    proc = subprocess.run(
        [
            sys.executable,
            str(DRIVER),
            "--task-id", "T-TOOL",
            "--project-id", "agent-kanban",
            "--base-url", url,
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "OMP_BIN": str(fake)},
        cwd=REPO_ROOT,
        timeout=90,
        check=False,
    )
    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"

    log = (AGENT_LOG_DIR / "T-TOOL.log").read_text()
    assert "[tool] read Reading sample.txt" in log
    assert "[tool] bash Running the gate" in log
    assert "[tool] bash FAILED" in log
    # the assistant text still lands after the tool activity
    assert "gate failed" in log
    # a tool-only stretch no longer looks dead: the log grew without any
    # assistant text before the final message
    before_text = log.split("gate failed")[0]
    assert "[tool]" in before_text and len(before_text.strip().splitlines()) >= 3

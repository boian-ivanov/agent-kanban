"""T-312 token/duration watchdog + AK-003 ownership/no-progress rules.

Runs examples/task-driver.py as a subprocess against a fake kanban API and
a fake omp binary (OMP_BIN). Each breach kind (max_tokens, max_duration,
no_progress, dot_only) is exercised end-to-end: the run is killed, an
evidence comment is posted, the task is moved back to approved, and the omp
child is reaped (no orphans).

AK-003:
  - no_progress fires only when a silent window ALSO burned tokens hard or
    the session is verifiably dead — silent + low-token + alive (healthy
    research) is never killed;
  - a driver whose run row belongs to another live pid logs 'not owner,
    exiting' and never interrupts/comments/moves;
  - claim is refused while another live driver owns the task.

Acceptance (T-312): "With max_tokens=10000 on a test task, run killed at
budget with evidence comment and task returns to approved; no orphan omp
processes after kill."
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
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
elif mode == "burn":
    # no text_delta (silent agent log) but tokens climb hard per window —
    # the AK-003 no_progress discriminator must treat this as churn
    total = 0
    for _ in range(200):
        total += 1000
        emit({"type": "message_end", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "burning"}],
            "usage": {"input": total // 2, "output": total // 2,
                      "totalTokens": total},
        }})
        time.sleep(0.01)
    emit({"type": "agent_end", "messages": [], "isTerminal": True})
    while sys.stdin.readline():
        pass
    sys.exit(0)
elif mode == "research":
    # AK-003: silent token burn at FAKE_TOKEN_RATE tokens/sec (no
    # text_delta — the agent log stays flat while tokens climb, exactly the
    # 2026-08-24 verifier-kill pattern: 1.09M tokens in 5 min of research).
    # The window-scaled no-progress token bar must classify a moderate rate
    # as healthy and a runaway rate as churn. Per-message usage is constant
    # (the driver ADDS each totalTokens to its running count).
    rate = int(os.environ.get("FAKE_TOKEN_RATE", "1000"))
    frame = max(rate // 10, 1)  # one usage frame per 0.1s
    while True:
        emit({"type": "message_end", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "researching"}],
            "usage": {"input": frame // 2, "output": frame // 2,
                      "totalTokens": frame},
        }})
        time.sleep(0.1)
elif mode == "dump":
    # record the initial prompt for prompt-content assertions, then finish;
    # exit when the driver closes stdin
    pf = os.environ.get("FAKE_PROMPT_FILE")
    if pf:
        with open(pf, "w") as f:
            f.write(obj.get("message", ""))
    emit({"type": "agent_end", "messages": [], "isTerminal": True})
    while sys.stdin.readline():
        pass
    sys.exit(0)
# silent: no output at all (healthy-research scenario for AK-003; breached
# only via max_duration in T-WD-DUR)

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
        # AK-003: run registry — POST /runs merges into .run; GET /runs
        # serves .run_row(). run_sequence optionally overrides GETs (last
        # entry repeats) to simulate another driver owning the task.
        self.run: dict = {}
        self.run_sequence: list[dict] = []
        self._runs_gets = 0

    def serve(self) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._server = server
        return f"http://127.0.0.1:{server.server_port}"

    def run_row(self) -> dict:
        row = self.run
        if self.run_sequence:
            row = self.run_sequence[min(self._runs_gets, len(self.run_sequence) - 1)]
        self._runs_gets += 1
        return row

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
                "kind": "task",
                "parent_id": None,
                "ancestors": [],
                "history": [],
            }

        def _context(self) -> dict:
            return {
                "task_id": "T-WD",
                "project_id": "agent-kanban",
                "task": self._task(),
                "ancestors": [
                    {
                        "id": "T-EPIC",
                        "kind": "epic",
                        "title": "Watchdog epic",
                        "description": "epic context for the driver prompt",
                        "acceptance": "epic acceptance",
                    }
                ],
                "comments": [
                    {
                        "id": 1,
                        "ts": "2026-08-24T00:00:00+00:00",
                        "actor": "omp",
                        "text": "note",
                    }
                ],
                "constraints": ["constraint one", "constraint two"],
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
                self._send(200, {"run": board.run_row()})
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
                body = self._read()
                board.run = {**board.run, **{k: v for k, v in body.items() if v}}
                self._send(200, {"run": board.run})
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
            "burn",
            (
                "--watchdog-window", "1",
                "--watchdog-min-growth", "1000",
                "--watchdog-token-climb", "10000",
            ),
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
    # evidence comment posted (elapsed, tokens, log bytes like --progress);
    # alive reflects the real omp state at breach time (never hardcoded)
    assert any(
        f"Budget breach ({breach})" in c
        and "[progress]" in c
        and "log=" in c
        and "alive=true" in c
        for c in b.comments
    ), f"comments: {b.comments}"
    if comment_marker:
        assert any(comment_marker in c for c in b.comments), f"comments: {b.comments}"
    # task returns to approved
    assert b.moves[-1] == "approved", f"moves: {b.moves}"
    # no orphan omp process after the kill
    assert not process_alive(pidfile), f"omp child {pidfile} still alive"


def test_silent_research_not_killed_by_no_progress(board, tmp_path):
    """AK-003: silent + low-token + alive is healthy research — the
    no_progress watchdog must NOT fire across several windows even when the
    agent log is flat (the 2026-08-24 false-positive incident pattern)."""
    b, url = board
    fake = tmp_path / "fake_omp.py"
    fake.write_text(FAKE_OMP)
    fake.chmod(0o755)
    pidfile = tmp_path / "silent.pid"
    env = {
        **os.environ,
        "OMP_BIN": str(fake),
        "FAKE_MODE": "silent",
        "FAKE_PIDFILE": str(pidfile),
    }
    cmd = [
        sys.executable,
        str(DRIVER),
        "--task-id", "T-SILENT",
        "--project-id", "agent-kanban",
        "--base-url", url,
        "--watchdog-window", "1",
        "--watchdog-min-growth", "1024",  # flat log window = silent
    ]
    proc = subprocess.Popen(
        cmd, env=env, cwd=REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        time.sleep(3.5)  # several watchdog windows; no breach expected
        assert proc.poll() is None, (
            "driver killed a healthy silent session:\n"
            f"{proc.stdout.read()}\n{proc.stderr.read()}"
        )
        assert process_alive(pidfile), "omp child died"
        assert b.comments == [], f"no breach comments expected: {b.comments}"
        assert b.moves == [], f"no moves expected: {b.moves}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        # the driver was SIGTERMed, so its finally never reaped the child
        if pidfile.exists():
            pid = int(pidfile.read_text().strip())
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        log = AGENT_LOG_DIR / "T-SILENT.log"
        log.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("rate", "expected_breach"),
    [
        (3000, None),  # 3K tok/s ~ 180K/min: healthy research band
        (20000, "no_progress"),  # 20K tok/s ~ 1.2M/min: runaway burn
    ],
)
def test_silent_research_token_burn_discriminated(
    board, tmp_path, rate, expected_breach
):
    """AK-003: silent + token-burning research is judged against the
    window-scaled token bar (400K tokens/min of silent burn). The 2026-08-24
    verifier-kill pattern (1.09M tokens in 5 min of silent reading = ~218K
    tok/min) must survive; a runaway burn must breach no_progress."""
    b, url = board
    fake = tmp_path / "fake_omp.py"
    fake.write_text(FAKE_OMP)
    fake.chmod(0o755)
    pidfile = tmp_path / "research.pid"
    env = {
        **os.environ,
        "OMP_BIN": str(fake),
        "FAKE_MODE": "research",
        "FAKE_TOKEN_RATE": str(rate),
        "FAKE_PIDFILE": str(pidfile),
    }
    # window=1s -> scaled bar = 400000 * 1 // 60 = 6666 tokens per window
    cmd = [
        sys.executable,
        str(DRIVER),
        "--task-id", "T-RES",
        "--project-id", "agent-kanban",
        "--base-url", url,
        "--watchdog-window", "1",
        "--watchdog-min-growth", "1024",  # silent window (log stays flat)
    ]
    proc = subprocess.Popen(
        cmd, env=env, cwd=REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        time.sleep(3.5)  # several watchdog windows
        if expected_breach is None:
            assert proc.poll() is None, (
                "driver killed a healthy silent research session:\n"
                f"{proc.stdout.read()}\n{proc.stderr.read()}"
            )
            assert process_alive(pidfile), "omp child died"
            assert b.comments == [], f"no breach comments expected: {b.comments}"
            assert b.moves == [], f"no moves expected: {b.moves}"
        else:
            rc = proc.wait(timeout=30)
            assert rc == 0, f"driver failed:\n{proc.stdout.read()}\n{proc.stderr.read()}"
            assert any(
                f"Budget breach ({expected_breach})" in c for c in b.comments
            ), f"comments: {b.comments}"
            assert b.moves[-1] == "approved", f"moves: {b.moves}"
            assert not process_alive(pidfile), f"omp child {pidfile} still alive"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if pidfile.exists():
            pid = int(pidfile.read_text().strip())
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        log = AGENT_LOG_DIR / "T-RES.log"
        log.unlink(missing_ok=True)


def test_non_owner_budget_breach_exits_without_touching(board, tmp_path):
    """AK-003: a driver whose run row now belongs to another live pid logs
    'not owner, exiting' and never interrupts/comments/moves — even when its
    own watchdog would breach (rogue-driver incident pattern)."""
    b, url = board
    # GET /runs: the pre-claim call sees no row (claim proceeds); from the
    # breach-time ownership check onward it serves another live driver's row.
    b.run_sequence = [
        {},
        {"status": "running", "pid": os.getpid()},
    ]
    proc, pidfile = run_driver(
        url, "T-NO", tmp_path, "--max-duration", "1", mode="silent"
    )
    assert proc.returncode == 4, f"driver stdout:\n{proc.stdout}\n{proc.stderr}"
    assert b.comments == [], f"non-owner must not comment: {b.comments}"
    assert b.moves == [], f"non-owner must not move: {b.moves}"
    log = AGENT_LOG_DIR / "T-NO.log"
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    assert "not owner, exiting" in text, text
    log.unlink(missing_ok=True)
    # own omp child is still reaped (no orphans)
    assert not process_alive(pidfile), f"omp child {pidfile} still alive"


def test_claim_refused_when_live_run_owns_task(board, tmp_path):
    """AK-003: the driver refuses to start (claim) when another live driver
    owns the task — exits before claiming, no comment/move."""
    b, url = board
    b.run_sequence = [{"status": "running", "pid": os.getpid()}]
    proc, _ = run_driver(url, "T-REF", tmp_path, mode="silent")
    assert proc.returncode == 2, f"driver stdout:\n{proc.stdout}\n{proc.stderr}"
    assert b.comments == [], f"no comments expected: {b.comments}"
    assert b.moves == [], f"no moves expected: {b.moves}"
    log = AGENT_LOG_DIR / "T-REF.log"
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    assert "not claiming" in text, text
    log.unlink(missing_ok=True)


def test_initial_prompt_injects_context_bundle(board, tmp_path):
    """T-313: the driver fetches GET /api/tasks/{id}/context and injects the
    rendered bundle (task fields, epic description, story acceptance, recent
    comments, shared constraints) into the initial omp prompt — replacing the
    old hardcoded protocol text."""
    b, url = board
    b.task_status = "testing"  # agent_end then completes the run immediately
    fake = tmp_path / "fake_omp.py"
    fake.write_text(FAKE_OMP)
    fake.chmod(0o755)
    prompt_file = tmp_path / "prompt.txt"
    env = {
        **os.environ,
        "OMP_BIN": str(fake),
        "FAKE_MODE": "dump",
        "FAKE_PROMPT_FILE": str(prompt_file),
    }
    cmd = [
        sys.executable,
        str(DRIVER),
        "--task-id",
        "T-WD",
        "--project-id",
        "agent-kanban",
        "--base-url",
        url,
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    prompt = prompt_file.read_text(encoding="utf-8")
    # task fields + ancestor chain (epic description/acceptance) + comments + constraints
    assert "Watchdog epic" in prompt
    assert "epic context for the driver prompt" in prompt
    assert "epic acceptance" in prompt
    assert "constraint one" in prompt and "constraint two" in prompt
    # the old hardcoded protocol text is gone (replaced by the bundle)
    assert "Read the task (title, description, acceptance)" not in prompt
    log = AGENT_LOG_DIR / "T-WD.log"
    log.unlink(missing_ok=True)

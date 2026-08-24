#!/usr/bin/env python3
"""Per-task agent driver: claims a kanban task and runs a full omp session.

Replaces the fire-and-forget ``omp --no-session`` pipeline that used to live
inside ``examples/launch-demo.sh`` (now a thin spawner). Runs the session in
omp ``--mode rpc`` (JSON-line protocol on stdin/stdout, see
``examples/omp-rpc-stream.py`` for the original RPC wrapper).

Flow:
  1. claim the task atomically via POST /api/tasks/{id}/claim
     (assignee=agent:<role>, approved -> in_progress, history-recorded);
     role is resolved from the task's assignee as before (agent:fe -> fe,
     none/unknown -> default),
  2. register a task_runs row (pid, started_at, model, role, control_port),
  3. run the omp session (``--mode rpc``; initial prompt + follow-up steers
     are sent as JSON-line ``prompt`` commands) for the task prompt
     (worktree from agents.json, overridable per project via the board's
     project.path / project.model),
  4. write each complete assistant message to task_chat via
     POST /api/tasks/{id}/chat (per message — not per raw text delta), while
     still archiving the raw token stream to
     kanban_data/agent-logs/{task_id}.log for the board SSE endpoint,
  5. expose a localhost control socket (port recorded in task_runs):
     - ``stop {reason, to_status}`` — SIGINT the omp session, then comment
       the reason and move the task (blocked for human intervention,
       approved for a routine auto-retry — D2);
     - ``steer {text, comment_id}`` — inject a user message into the live
       session (``mode: followUp`` so it queues behind the running turn);
     - ``ping`` / ``status`` / ``budget`` — live watchdog state (tokens
       used, elapsed, limits; T-312).
     Comments starting with ``@agent`` also steer (board rules engine
     forwards them to this socket); the driver additionally polls
     GET /api/tasks/{id}?since_seq every 5s as a fallback so steering keeps
     working when the rule path is unavailable,
  6. enforce per-task budgets (T-312): max_tokens (default 30M, D5),
     max_duration (default 60 min), a no-progress watchdog (<1KB agent-log
     growth per 5 min while alive = churn) and dot-only output detection
     (T-284 500M-token incident). On breach: kill the session, comment
     evidence (elapsed, tokens, log bytes — same as orchestrator.py
     --progress) and move the task back to approved,
  7. mark the run done/failed/stopped in task_runs and exit. A run is
     finished when a turn completes and the task has left in_progress
     (the agent's normal final move is to testing).

Usage:
  task-driver.py --task-id T-310 --project-id agent-kanban [--base-url URL]
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_LOG_DIR = REPO_ROOT / "kanban_data" / "agent-logs"
DEFAULT_AGENTS_JSON = REPO_ROOT / "examples" / "agents.json"
DEFAULT_BASE_URL = os.environ.get("KANBAN_URL", "http://127.0.0.1:7777")
# Overridable for tests: a fake omp binary that speaks the RPC protocol.
OMP_BIN = os.environ.get("OMP_BIN", "omp")

# Fallback comment poll interval (steering survives a dead rules path).
POLL_INTERVAL = 5.0
# How long to wait after SIGINT before escalating to SIGTERM/SIGKILL.
SIGINT_GRACE = 10.0
# T-312 watchdog budgets (D5-locked trial values; tune per role via
# agents.json or per run via CLI flags):
DEFAULT_MAX_TOKENS = 30_000_000  # 30M tokens per task run
DEFAULT_MAX_DURATION_S = 3600  # 60 min per task run
NO_PROGRESS_WINDOW_S = 300  # no-progress check window (5 min)
NO_PROGRESS_MIN_GROWTH = 1024  # <1KB agent-log growth in window = churn
DOT_RUN_BYTES = 1024  # 1024 consecutive dot chars = dot-only churn (T-284)
WATCH_TICK_S = 1.0  # budget watchdog tick


class ApiError(RuntimeError):
    """API call failed; carries the HTTP status for 409 handling."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"API {status}: {detail}")
        self.status = status
        self.detail = detail


def api_json(
    base_url: str, method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail", str(e))
        except (ValueError, AttributeError):
            detail = str(e)
        raise ApiError(e.code, detail) from None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def classify_steer(text: str) -> tuple[str, str, str] | None:
    """Classify board steer / comment text.

    Returns ``(kind, to_status, payload)`` with kind ``"stop"`` or
    ``"steer"`` (``to_status`` only matters for stops), or None when the
    text is not a steering directive at all.

    - ``"@agent stop[: reason]"`` / ``"@agent stop blocked: reason"``
      -> stop; an explicit ``blocked``/``approved`` word selects the
      post-stop status (D2: blocked = human intervention, approved =
      routine auto-retry), default blocked;
    - ``"@agent <instruction>"`` -> steer with the instruction (prefix
      stripped);
    - any other non-empty text (plain API steer) -> steer with the raw text.
    """
    t = text.strip()
    if not t:
        return None
    if t.startswith("@agent"):
        rest = t[len("@agent") :].strip()
        if rest.startswith("stop"):
            tail = rest[len("stop") :].strip()
            to_status = "blocked"
            head = tail.split(None, 1)
            if head and head[0].rstrip(":") in ("blocked", "approved"):
                to_status = head[0].rstrip(":")
                tail = head[1] if len(head) > 1 else ""
            reason = tail.lstrip(":").strip() or "stopped by user"
            return ("stop", to_status, reason)
        if rest:
            return ("steer", "", rest.lstrip(":").strip())
        return None
    return ("steer", "", t)


class ControlServer:
    """Localhost control socket; port is recorded in task_runs.

    T-310 exposed the socket (ping/status answer); T-311 adds stop/steer;
    T-312 adds ``budget`` (live watchdog state). Unknown commands get
    ``not_implemented`` so the wire protocol stays stable.
    """

    def __init__(self, *, task_id: str, model: str, role: str, pid: int) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(0.5)
        self.port: int = self._sock.getsockname()[1]
        self._meta = {"task_id": task_id, "model": model, "role": role, "pid": pid}
        self._stop = threading.Event()
        self._steers: queue.Queue[tuple[str, int | None]] = queue.Queue()
        self._stop_lock = threading.Lock()
        self._stop_request: dict[str, Any] | None = None
        self._budget: dict[str, Any] | None = None
        self._thread = threading.Thread(target=self._run, name="control", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            readable, _, _ = select.select([self._sock], [], [], 0.5)
            if not readable:
                continue
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            self._serve(conn)

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            req = self._read_request(conn)
            if req is None:
                return
            resp = self._handle(req)
            try:
                conn.sendall((json.dumps(resp) + "\n").encode())
            except OSError:
                pass

    @staticmethod
    def _read_request(conn: socket.socket) -> dict[str, Any] | None:
        try:
            conn.settimeout(5)
            data = conn.recv(65536)
            return json.loads(data.decode())
        except (OSError, ValueError):
            return None

    def _handle(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd")
        if cmd in ("ping", "status"):
            return {
                "ok": True,
                "cmd": cmd,
                "status": "stopping" if self._stop_request else "running",
                **self._meta,
            }
        if cmd == "stop":
            with self._stop_lock:
                self._stop_request = {
                    "reason": str(req.get("reason") or ""),
                    "to_status": req.get("to_status") or "blocked",
                }
            return {"ok": True, "cmd": "stop", "queued": True}
        if cmd == "steer":
            text = str(req.get("text") or "").strip()
            if not text:
                return {"ok": False, "cmd": "steer", "error": "empty text"}
            self._steers.put((text, req.get("comment_id")))
            return {"ok": True, "cmd": "steer", "queued": True}
        if cmd == "budget":
            return {
                "ok": True,
                "cmd": "budget",
                **self._meta,
                "budget": self._budget or {},
            }
        return {"ok": False, "cmd": cmd, "error": "not_implemented"}

    def get_stop_request(self) -> dict[str, Any] | None:
        with self._stop_lock:
            return self._stop_request

    def next_steer(self) -> tuple[str, int | None] | None:
        try:
            return self._steers.get_nowait()
        except queue.Empty:
            return None

    def set_budget(self, budget: dict[str, Any]) -> None:
        """Share the live watchdog state with the control socket."""
        self._budget = budget

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._sock.close()
        except OSError:
            pass


def _message_text(message: dict[str, Any]) -> str:
    """Full text of an assistant message (all text blocks, newline-joined)."""
    parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text") or ""
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _interrupt(proc: subprocess.Popen) -> None:
    """SIGINT the omp session and make sure it is gone (no orphans)."""
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
    except OSError:
        pass
    try:
        proc.wait(timeout=SIGINT_GRACE)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description="per-task agent driver")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--agents-json", default=str(DEFAULT_AGENTS_JSON))
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="token budget per run (0 disables; default 30M, role config overrides)",
    )
    parser.add_argument(
        "--max-duration",
        type=int,
        default=None,
        help="duration budget in seconds (0 disables; default 3600, role config overrides)",
    )
    parser.add_argument(
        "--watchdog-window",
        type=int,
        default=NO_PROGRESS_WINDOW_S,
        help="no-progress check window in seconds",
    )
    parser.add_argument(
        "--watchdog-min-growth",
        type=int,
        default=NO_PROGRESS_MIN_GROWTH,
        help="minimum agent-log byte growth per window; less while alive = churn",
    )
    args = parser.parse_args()

    AGENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = AGENT_LOG_DIR / f"{args.task_id}.log"

    def log_line(text: str) -> None:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{_now_iso()}] {text}\n")

    def log_delta(text: str) -> None:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(text)
            f.flush()

    # --- task + role resolution (same rules as the old launcher) ---
    task = api_json(args.base_url, "GET", f"/api/tasks/{args.task_id}")
    assignee = task.get("assignee") or ""
    role = assignee[len("agent:") :] if assignee.startswith("agent:") else "default"

    agents = json.loads(Path(args.agents_json).read_text(encoding="utf-8"))
    cfg = agents.get(role) or agents.get("default") or {}
    model = cfg.get("model") or "opencode-go/deepseek-v4-flash"
    worktree = cfg.get("worktree") or str(REPO_ROOT)
    role_prompt = cfg.get("prompt") or ""
    constraints = "\n".join(f"- {c}" for c in agents.get("constraints", []))
    # T-312 budgets: CLI > role config (agents.json) > D5-locked defaults.
    # 0/None disables the respective limit.
    cfg_max_tokens = cfg.get("max_tokens")
    cfg_max_duration = cfg.get("max_duration")
    max_tokens = (
        args.max_tokens
        if args.max_tokens is not None
        else (cfg_max_tokens if cfg_max_tokens is not None else DEFAULT_MAX_TOKENS)
    )
    max_duration_s = (
        args.max_duration
        if args.max_duration is not None
        else (
            cfg_max_duration if cfg_max_duration is not None else DEFAULT_MAX_DURATION_S
        )
    )
    watchdog_window = args.watchdog_window
    watchdog_min_growth = args.watchdog_min_growth

    board = (
        api_json(args.base_url, "GET", f"/api/board?project={args.project_id}").get(
            "project"
        )
        or {}
    )
    if board.get("path"):
        worktree = board["path"]
    if board.get("model"):
        model = board["model"]
    Path(worktree).mkdir(parents=True, exist_ok=True)

    log_line(
        f"omp dispatch for {args.task_id} ({args.project_id}) role={role} "
        f"model={model} worktree={worktree}"
    )

    # --- claim (atomic: assignee=agent:<role> + approved -> in_progress) ---
    try:
        api_json(
            args.base_url,
            "POST",
            f"/api/tasks/{args.task_id}/claim",
            {"assignee": f"agent:{role}"},
        )
    except ApiError as e:
        log_line(f"claim failed: {e}")
        return 2

    # --- control socket + task_runs registration ---
    control = ControlServer(
        task_id=args.task_id, model=model, role=role, pid=os.getpid()
    )
    try:
        api_json(
            args.base_url,
            "POST",
            f"/api/tasks/{args.task_id}/runs",
            {
                "pid": os.getpid(),
                "started_at": _now_iso(),
                "model": model,
                "role": role,
                "control_port": control.port,
                "status": "running",
            },
        )
    except ApiError as e:
        log_line(f"run registration failed: {e}")
    budget_state: dict[str, Any] = {
        "max_tokens": max_tokens,
        "max_duration_s": max_duration_s,
        "tokens": 0,
        "elapsed_s": 0,
        "log_bytes": 0,
        "breach": None,
    }
    control.set_budget(budget_state)

    prompt_text = (
        f"You are a kanban worker agent dispatched by the local agent-kanban "
        f"board (project {args.project_id}). Task: {args.task_id} (role: {role}).\n\n"
        f"{role_prompt}\n\n"
        f"Constraints:\n{constraints}\n\n"
        f"Board protocol:\n"
        f"1. Read the task (title, description, acceptance): "
        f"curl -s {args.base_url}/api/tasks/{args.task_id}\n"
        f"2. Mark it in progress: curl -s -X POST "
        f"{args.base_url}/api/tasks/{args.task_id}/move "
        f"-H 'Content-Type: application/json' -d '{{\"to_status\":\"in_progress\"}}'\n"
        f"3. Do the work in the current directory.\n"
        f"4. Post a summary comment: curl -s -X POST "
        f"{args.base_url}/api/tasks/{args.task_id}/comment "
        f"-H 'Content-Type: application/json' -d '{{\"text\":\"<summary>\"}}'\n"
        f"5. Move the task to testing: curl -s -X POST "
        f"{args.base_url}/api/tasks/{args.task_id}/move "
        f"-H 'Content-Type: application/json' -d '{{\"to_status\":\"testing\"}}'\n\n"
        f"The task content defines the actual work; the API calls above are the "
        f"board protocol. Reply with a brief summary of what you did."
    )

    # --- run the omp session (--mode rpc: JSON-line protocol on stdin/stdout) ---
    proc = subprocess.Popen(
        [OMP_BIN, "--mode", "rpc", "--no-session", "--model", model],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=worktree,
        text=True,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    def send(obj: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    read_buf: str = ""

    def read_line(timeout: float = 5.0) -> str | None:
        """One JSON line from omp stdout.

        select() only sees the OS pipe: when a burst of lines arrives in a
        single read, Python's BufferedReader holds the rest and select goes
        quiet, stalling the loop (T-312 watchdog exposed this). Read the raw
        fd into our own buffer instead.
        """
        nonlocal read_buf
        while True:
            if "\n" in read_buf:
                line, read_buf = read_buf.split("\n", 1)
                return line + "\n"
            if select.select([proc.stdout], [], [], timeout)[0]:
                chunk = os.read(proc.stdout.fileno(), 65536).decode(
                    "utf-8", errors="replace"
                )
                if not chunk:
                    return None  # EOF
                read_buf += chunk
            else:
                return None

    def write_chat(role: str, content: str) -> None:
        try:
            api_json(
                args.base_url,
                "POST",
                f"/api/tasks/{args.task_id}/chat",
                {"role": role, "content": content},
            )
        except ApiError as e:
            log_line(f"chat write failed: {e}")

    # --- steering state ---
    steered_ids: set[int] = set()
    last_seq = max(((h.get("id") or 0) for h in (task.get("history") or [])), default=0)
    last_poll = 0.0
    stop_request: dict[str, Any] | None = None

    def handle_steer(text: str, comment_id: int | None) -> None:
        """Act on control-socket / polled steer text. A stop directive sets
        stop_request (the main loop interrupts the session); a plain steer
        is injected as a follow-up user turn."""
        nonlocal stop_request
        if comment_id is not None:
            if comment_id in steered_ids:
                return
            steered_ids.add(comment_id)
        parsed = classify_steer(text)
        if parsed is None:
            return
        kind, to_status, payload = parsed
        if kind == "stop":
            stop_request = {"reason": payload, "to_status": to_status}
            log_line(f"stop requested: {payload}")
            return
        # steer — queue as the next turn; record what the agent sees
        send({"type": "prompt", "message": payload, "mode": "followUp"})
        log_line(f"steer injected: {payload[:120]}")
        write_chat("user", payload)

    def poll_comments() -> None:
        """Fallback steering: new comments since last_seq (5s poll)."""
        nonlocal last_seq
        try:
            fresh = api_json(
                args.base_url,
                "GET",
                f"/api/tasks/{args.task_id}?since_seq={last_seq}",
            )
        except ApiError as e:
            log_line(f"comment poll failed: {e}")
            return
        history = fresh.get("history") or []
        if not history:
            return
        last_seq = max(last_seq, max(h.get("id") or 0 for h in history))
        for h in history:
            if h.get("action") != "comment":
                continue
            text = h.get("comment") or ""
            cid = h.get("id")
            if cid in steered_ids:
                continue
            if classify_steer(text) is not None:
                log_line(f"poll: steering comment #{cid}")
                handle_steer(text, cid)

    run_status = "failed"
    budget_breach: str | None = None
    tokens_used = 0
    t0 = time.monotonic()
    dot_run = 0
    prev_log_bytes = log_path.stat().st_size if log_path.exists() else 0
    last_watch = t0
    last_progress_check = t0
    try:
        ready = read_line(timeout=15)
        if not ready or not ready.strip():
            log_line("[ERROR] omp did not send ready event")
        else:
            # drain startup frames (command lists, ui requests), then start
            while read_line(timeout=0.3):
                pass
            t0 = time.monotonic()
            last_watch = t0
            last_progress_check = t0
            prev_log_bytes = log_path.stat().st_size if log_path.exists() else 0
            send({"type": "prompt", "message": prompt_text})
            log_line("omp session started; initial prompt sent")

            done = False
            while not done:
                # 0. budget watchdog (T-312): duration and no-progress are
                #    checked here; tokens/dot-only fire in the event handlers
                if budget_breach is None:
                    now = time.monotonic()
                    if now - last_watch >= WATCH_TICK_S:
                        last_watch = now
                        elapsed_s = int(now - t0)
                        budget_state["elapsed_s"] = elapsed_s
                        if max_duration_s and elapsed_s >= max_duration_s:
                            budget_breach = "max_duration"
                        elif (
                            now - last_progress_check >= watchdog_window
                            and proc.poll() is None
                        ):
                            cur_bytes = (
                                log_path.stat().st_size if log_path.exists() else 0
                            )
                            budget_state["log_bytes"] = cur_bytes
                            if cur_bytes - prev_log_bytes < watchdog_min_growth:
                                budget_breach = "no_progress"
                            prev_log_bytes = cur_bytes
                            last_progress_check = now
                if budget_breach is not None:
                    log_line(f"budget breach: {budget_breach}")
                    budget_state["breach"] = budget_breach
                    _interrupt(proc)
                    run_status = "stopped"
                    break

                # 1. stop request (control socket or steering comment)
                if stop_request is None:
                    stop_request = control.get_stop_request()
                if stop_request is not None:
                    _interrupt(proc)
                    run_status = "stopped"
                    break

                # 2. queued steers (control socket)
                steer = control.next_steer()
                if steer is not None:
                    handle_steer(steer[0], steer[1])
                    continue

                # 3. events
                line = read_line(timeout=0.5)
                if line is None:
                    now = time.monotonic()
                    if now - last_poll >= POLL_INTERVAL:
                        last_poll = now
                        poll_comments()
                    continue
                if not line.strip():
                    # EOF: omp closed stdout without agent_end — failure
                    proc.wait(timeout=10)
                    log_line(f"[ERROR] omp exited (code {proc.returncode})")
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = obj.get("type")
                if kind == "message_update":
                    ev = obj.get("assistantMessageEvent") or {}
                    if ev.get("type") == "text_delta":
                        delta = ev.get("delta") or ""
                        if delta:
                            log_delta(delta)  # raw archive (SSE streams this)
                            sys.stdout.write(delta)
                            sys.stdout.flush()
                            # dot-only churn (T-284): 1024 consecutive dots
                            # = the model is burning tokens, not working
                            if dot_run is not None:
                                for ch in delta:
                                    if ch == ".":
                                        dot_run += 1
                                        if dot_run >= DOT_RUN_BYTES:
                                            budget_breach = "dot_only"
                                            dot_run = None
                                            break
                                    else:
                                        dot_run = 0
                elif kind == "message_end":
                    message = obj.get("message") or {}
                    if message.get("role") == "assistant":
                        content = _message_text(message)
                        if content:
                            write_chat(f"agent:{role}", content)
                        # per-request usage: the session total burned so far
                        usage = message.get("usage") or {}
                        total = usage.get("totalTokens")
                        if isinstance(total, (int, float)) and total > 0:
                            tokens_used += int(total)
                            budget_state["tokens"] = tokens_used
                            if max_tokens and tokens_used >= max_tokens:
                                budget_breach = "max_tokens"
                    log_delta("\n")
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                elif kind == "agent_end":
                    log_delta("\n")
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    # turn done — natural completion: the agent moves the
                    # task out of in_progress when finished. Stay alive
                    # otherwise (steers/stops may still arrive).
                    try:
                        fresh = api_json(
                            args.base_url, "GET", f"/api/tasks/{args.task_id}"
                        )
                        if fresh.get("status") != "in_progress":
                            run_status = "done"
                            done = True
                    except ApiError as e:
                        log_line(f"status check failed: {e}")
    finally:
        # always reap the omp process (no orphans); closing stdin lets a
        # healthy rpc session exit gracefully.
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    # --- budget breach bookkeeping: comment evidence, move back to approved ---
    if budget_breach is not None:
        elapsed_min = (time.monotonic() - t0) / 60
        log_bytes = log_path.stat().st_size if log_path.exists() else 0
        status = "in_progress"
        try:
            fresh = api_json(args.base_url, "GET", f"/api/tasks/{args.task_id}")
            status = fresh.get("status") or status
        except ApiError:
            pass
        budget_state["breach"] = budget_breach
        evidence = (
            f"[progress] {args.task_id} status={status} "
            f"elapsed={elapsed_min:.0f}m tokens={tokens_used}/{max_tokens} "
            f"log={log_bytes}B alive=false"
        )
        text = (
            f"Budget breach ({budget_breach}): {evidence} — killed session, "
            f"moved back to approved for retry."
        )
        try:
            api_json(
                args.base_url,
                "POST",
                f"/api/tasks/{args.task_id}/comment",
                {"text": text},
            )
        except ApiError as e:
            log_line(f"budget comment failed: {e}")
        try:
            api_json(
                args.base_url,
                "POST",
                f"/api/tasks/{args.task_id}/move",
                {"to_status": "approved"},
            )
        except ApiError as e:
            log_line(f"budget move failed: {e}")

    # --- stop bookkeeping: comment the reason, move per D2 ---
    if stop_request is not None:
        reason = stop_request.get("reason") or "stopped"
        to_status = stop_request.get("to_status") or "blocked"
        try:
            api_json(
                args.base_url,
                "POST",
                f"/api/tasks/{args.task_id}/comment",
                {"text": f"Agent stopped: {reason}"},
            )
        except ApiError as e:
            log_line(f"stop comment failed: {e}")
        try:
            api_json(
                args.base_url,
                "POST",
                f"/api/tasks/{args.task_id}/move",
                {"to_status": to_status},
            )
        except ApiError as e:
            log_line(f"stop move failed: {e}")

    log_line(f"run finished for {args.task_id} status={run_status}")
    try:
        api_json(
            args.base_url,
            "POST",
            f"/api/tasks/{args.task_id}/runs",
            {"ended_at": _now_iso(), "status": run_status},
        )
    except ApiError as e:
        log_line(f"run finish failed: {e}")
    finally:
        control.close()

    return 0 if run_status in ("done", "stopped") else 1


if __name__ == "__main__":
    sys.exit(main())

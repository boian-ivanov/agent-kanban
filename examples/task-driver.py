#!/usr/bin/env python3
"""Per-task agent driver: claims a kanban task and runs a full omp session.

Replaces the fire-and-forget ``omp --no-session`` pipeline that used to live
inside ``examples/launch-demo.sh`` (now a thin spawner). Built on
``examples/omp-rpc-stream.py`` (omp ``--mode rpc`` JSON-line protocol).

Flow:
  1. claim the task atomically via POST /api/tasks/{id}/claim
     (assignee=agent:<role>, approved -> in_progress, history-recorded);
     role is resolved from the task's assignee as before (agent:fe -> fe,
     none/unknown -> default),
  2. register a task_runs row (pid, started_at, model, role, control_port),
   3. run the omp session (``--mode json`` streams message events to stdout)
    for the task prompt (worktree from agents.json,
         overridable per project via the board's project.path / project.model),
  4. write each complete assistant message to task_chat via
     POST /api/tasks/{id}/chat (per message — not per raw text delta), while
     still archiving the raw token stream to
     kanban_data/agent-logs/{task_id}.log for the board SSE endpoint,
  5. expose a localhost control socket (port recorded in task_runs) for
     stop/steer/budget commands — the command set is filled in by T-311/T-312;
     ping/status already answer,
  6. mark the run done/failed in task_runs and exit.

Usage:
  task-driver.py --task-id T-310 --project-id agent-kanban [--base-url URL]
"""

from __future__ import annotations

import argparse
import json
import os
import select
import socket
import subprocess
import sys
import threading
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


class ControlServer:
    """Localhost control socket; port is recorded in task_runs.

    T-310 exposes the socket (ping/status answer). stop/steer/budget command
    handlers are added by T-311/T-312; unknown commands get
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
                "status": "stopping" if self._stop.is_set() else "running",
                **self._meta,
            }
        return {"ok": False, "cmd": cmd, "error": "not_implemented"}

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


def main() -> int:
    parser = argparse.ArgumentParser(description="per-task agent driver")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--agents-json", default=str(DEFAULT_AGENTS_JSON))
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

    # --- run the omp session (--mode json: prompt as argv, event stream out) ---
    proc = subprocess.Popen(
        [OMP_BIN, "--mode", "json", "--no-session", "--model", model, prompt_text],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=worktree,
        text=True,
    )
    assert proc.stdout is not None

    def read_line(timeout: float = 5.0) -> str | None:
        if select.select([proc.stdout], [], [], timeout)[0]:
            return proc.stdout.readline()
        return None

    run_status = "failed"
    try:
        done = False
        while not done:
            line = read_line(timeout=0.5)
            if line is None:
                continue
            if not line.strip():
                # EOF: omp closed stdout without agent_end — treat as failure
                proc.wait(timeout=10)
                log_line(
                    f"[ERROR] omp exited (code {proc.returncode}) before agent_end"
                )
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
            elif kind == "message_end":
                message = obj.get("message") or {}
                if message.get("role") == "assistant":
                    content = _message_text(message)
                    if content:
                        try:
                            api_json(
                                args.base_url,
                                "POST",
                                f"/api/tasks/{args.task_id}/chat",
                                {"role": f"agent:{role}", "content": content},
                            )
                        except ApiError as e:
                            log_line(f"chat write failed: {e}")
                    log_delta("\n")
                    sys.stdout.write("\n")
                    sys.stdout.flush()
            elif kind == "agent_end":
                done = True
                run_status = "done"
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

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

    return 0 if run_status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())

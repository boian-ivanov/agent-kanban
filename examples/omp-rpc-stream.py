#!/usr/bin/env python3
"""
Drive omp in RPC mode, polling for assistant text so the kanban board
can stream agent output in near-real-time via the SSE endpoint.

Usage:
  omp-rpc-stream.py --model <model> --cwd <dir> [task prompt file or text]

Writes agent stdout/stderr to <kanban_data>/agent-logs/<task_id>.log
(line-buffered) and the final prompt result to stdout (captured by
the launcher's group redirect).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time

AGENT_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "kanban_data",
    "agent-logs",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="omp RPC streaming wrapper")
    parser.add_argument("--model", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("prompt_file", nargs="?", help="file containing the prompt, or stdin")
    args = parser.parse_args()

    os.makedirs(AGENT_LOG_DIR, exist_ok=True)
    log_path = os.path.join(AGENT_LOG_DIR, f"{args.task_id}.log")

    # Read prompt from file or stdin
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as f:
            prompt = f.read()
    else:
        prompt = sys.stdin.read()

    # Start omp in RPC mode
    proc = subprocess.Popen(
        ["omp", "--mode", "rpc", "--no-session", "--model", args.model],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=args.cwd,
        text=True,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    def send(obj: dict) -> None:
        line = json.dumps(obj)
        proc.stdin.write(line + "\n")
        proc.stdin.flush()

    def read_line(timeout: float = 5.0) -> str | None:
        """Non-blocking readline with timeout.  Returns None on timeout."""
        import select

        if select.select([proc.stdout], [], [], timeout)[0]:
            return proc.stdout.readline()
        return None

    def log_write(text: str) -> None:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(text)
            f.flush()

    try:
        # Consume ready event
        ready = read_line(timeout=10)
        if not ready:
            log_write("[ERROR] omp did not send ready event\n")
            return

        # Consume initial UI / commands frames (non-blocking drain)
        while True:
            line = read_line(timeout=0.3)
            if line is None:
                break

        # Send the prompt
        send({"type": "prompt", "message": prompt})

        # Poll for text updates while the prompt is in flight
        last_text: str | None = None
        prompt_done = False

        while not prompt_done:
            line = read_line(timeout=0.5)
            if line is None:
                # No new event from omp — poll for text
                if not prompt_done:
                    send({"type": "get_last_assistant_text"})
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            kind = obj.get("type")
            cmd = obj.get("command")
            if kind == "response" and cmd == "prompt":
                prompt_done = True
            elif kind == "response" and cmd == "get_last_assistant_text" and obj.get("success"):
                data = obj.get("data") or {}
                text = data.get("text")
                if text and text != last_text:
                    # Emit the delta (everything newer than last_text)
                    if last_text is None:
                        delta = text
                    elif text.startswith(last_text):
                        delta = text[len(last_text) :]
                    else:
                        delta = text
                    if delta:
                        log_write(delta)
                        sys.stdout.write(delta)
                        sys.stdout.flush()
                    last_text = text

        # After prompt done, one final poll for text
        if last_text:
            log_write("\n")
            sys.stdout.write("\n")
            sys.stdout.flush()

    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()

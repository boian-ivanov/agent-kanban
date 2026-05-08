"""Tool-calling demo against Ollama's native /api/chat (no OpenAI SDK).

Usage:
    pip install httpx
    ollama pull hermes3:8b           # any model with tool-calling support
    MODEL=hermes3:8b python examples/llm-tool-calling/ollama_demo.py
"""
from __future__ import annotations

import json
import os

import httpx

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
KANBAN = os.environ.get("KANBAN_URL", "http://localhost:7777")
MODEL  = os.environ.get("MODEL", "hermes3:8b")


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "kanban_list",
            "description": "List tasks in a project.",
            "parameters": {
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kanban_create",
            "description": "Create a new task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "title":      {"type": "string"},
                },
                "required": ["project_id", "title"],
            },
        },
    },
]


def dispatch(name: str, args: dict) -> str:
    if name == "kanban_list":
        r = httpx.get(f"{KANBAN}/api/board", params={"project": args["project_id"]})
        return json.dumps(r.json(), ensure_ascii=False)
    if name == "kanban_create":
        r = httpx.post(f"{KANBAN}/api/tasks", json={
            "title": args["title"], "project_id": args["project_id"],
        })
        return json.dumps(r.json(), ensure_ascii=False)
    return json.dumps({"error": f"unknown tool: {name}"})


def run(user_message: str) -> str:
    messages = [
        {"role": "system", "content": "Use the tools to manage the kanban."},
        {"role": "user", "content": user_message},
    ]
    while True:
        r = httpx.post(f"{OLLAMA}/api/chat", json={
            "model": MODEL, "messages": messages, "tools": TOOLS, "stream": False,
        }, timeout=180)
        r.raise_for_status()
        msg = r.json()["message"]
        messages.append(msg)
        if not msg.get("tool_calls"):
            return msg.get("content", "")
        for tc in msg["tool_calls"]:
            name = tc["function"]["name"]
            args = tc["function"]["arguments"]  # уже dict в Ollama
            if isinstance(args, str):
                args = json.loads(args)
            result = dispatch(name, args)
            messages.append({"role": "tool", "content": result})


if __name__ == "__main__":
    print(run("List the backlog of project 'default' and create a task 'Wired through Ollama'."))

"""Tool-calling demo: OpenAI Python SDK → any OpenAI-compatible endpoint.

Works with:
- OpenAI itself (set OPENAI_API_KEY only)
- Ollama (OPENAI_BASE_URL=http://localhost:11434/v1, OPENAI_API_KEY=ollama)
- vLLM (--enable-auto-tool-choice)
- llama.cpp server (--api-key any)
- Hermes-3 / Llama 3.1 / Mistral fine-tunes via any of the above

Usage:
    pip install openai httpx
    OPENAI_BASE_URL=http://localhost:11434/v1 \\
    OPENAI_API_KEY=ollama \\
    MODEL=hermes3:8b \\
    python examples/llm-tool-calling/openai_sdk_demo.py
"""
from __future__ import annotations

import json
import os

import httpx
from openai import OpenAI

KANBAN = os.environ.get("KANBAN_URL", "http://localhost:7777")
MODEL = os.environ.get("MODEL", "gpt-4o-mini")
client = OpenAI()  # uses OPENAI_BASE_URL / OPENAI_API_KEY env vars


# ---------------------------------------------------------------------------
# Tool catalog: subset of REST API exposed as function-calling tools.
# Add more from /openapi.json as needed.
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "kanban_list",
            "description": "List tasks in a project, grouped by column.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project slug"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kanban_create",
            "description": "Create a new task in the given project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "title":      {"type": "string"},
                    "priority":   {"type": "string", "enum": ["low", "normal", "high"]},
                    "size":       {"type": "string", "enum": ["S", "M", "L"]},
                },
                "required": ["project_id", "title"],
            },
        },
    },
]


def dispatch(name: str, args: dict) -> dict:
    """Map tool name → REST call. Return JSON for the LLM."""
    if name == "kanban_list":
        r = httpx.get(f"{KANBAN}/api/board", params={"project": args["project_id"]})
        r.raise_for_status()
        # сжимаем response чтобы не съесть контекст модели
        data = r.json()
        return {
            "project": data.get("project", {}).get("name"),
            "by_column": {
                col: [{"id": t["id"], "title": t["title"], "priority": t["priority"]}
                      for t in tasks]
                for col, tasks in data.get("tasks", {}).items() if tasks
            },
        }
    if name == "kanban_create":
        r = httpx.post(f"{KANBAN}/api/tasks", json={
            "title": args["title"],
            "project_id": args["project_id"],
            "priority": args.get("priority", "normal"),
            "size":     args.get("size", "M"),
        })
        r.raise_for_status()
        t = r.json()
        return {"id": t["id"], "title": t["title"], "status": t["status"]}
    raise ValueError(f"unknown tool: {name}")


def run(user_message: str) -> str:
    messages = [
        {"role": "system", "content":
            "You manage a local kanban board. Use the tools to query and create tasks."},
        {"role": "user", "content": user_message},
    ]
    while True:
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS,
        )
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_unset=True))
        if not msg.tool_calls:
            return msg.content or ""
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            result = dispatch(tc.function.name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })


if __name__ == "__main__":
    print(run("Show me the backlog of project 'default', then create a task 'Try the LLM demo' there."))

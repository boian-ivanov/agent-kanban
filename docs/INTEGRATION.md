# Connecting AI agents to agent-kanban

Three integration paths exist; pick the one your agent supports:

1. **MCP server (stdio)** — easiest for Claude Code, Cline. Works out of the box.
2. **Built-in REST + auto-generated OpenAPI** — for opencode, Open WebUI, any HTTP-aware agent.
3. **Function-calling LLM** — define OpenAI-compatible tools yourself; the kanban server stays REST-only.

The kanban server is the same in all three cases — the difference is only how the agent discovers and invokes its endpoints.

---

## 1. Claude Code (MCP)

**Setup.** Add a project-scope `.mcp.json` in your repo root (or update `~/.claude.json` for global access):

```jsonc
{
  "mcpServers": {
    "agent-kanban": {
      "type": "stdio",
      "command": "/abs/path/to/agent-kanban/.venv/bin/python",
      "args":    ["-m", "kanban_mcp"],
      "cwd":     "/abs/path/to/agent-kanban",
      "env": {
        "PYTHONPATH": "/abs/path/to/agent-kanban",
        "KANBAN_DB":  "/abs/path/to/agent-kanban/tasks.db",
        "KANBAN_PROJECT_ID": "myproj",
        "KANBAN_ACTOR": "claude"
      }
    }
  }
}
```

**`PYTHONPATH` is required**: Claude Code launches the stdio MCP server while ignoring the `cwd` field — without `PYTHONPATH`, python won't find the `kanban_mcp` module and MCP fails with `Failed to connect`.

`KANBAN_DB` — absolute path to the SQLite file (especially important if Claude Code and the kanban live in different directories).
`KANBAN_PROJECT_ID` — default project for `kanban_create` calls without an argument.
`KANBAN_ACTOR` — author name written into `task_history` for every move/comment the agent makes. Set this to something other than `user` (e.g. `claude`) so the history clearly distinguishes agent actions from human drag-drops in the UI.

Restart Claude Code (or run `claude mcp list` to confirm `✓ Connected`). 14 tools become available:

| Tool | Purpose |
|---|---|
| `kanban_columns` | list of columns + ownership semantics |
| `kanban_list` | tasks with optional status / assignee filters |
| `kanban_get` | full card with history + links + blockers |
| `kanban_pull` | atomically claim an `approved` task → `analyst`, assignee = current agent |
| `kanban_move` | move card to a new column |
| `kanban_comment` | append comment to history |
| `kanban_create` | new card |
| `kanban_link` | attach memory/file/pr/url link |
| `kanban_blockers` | set/replace inter-task blockers |
| `kanban_update` | edit title/priority/size/description/blocker |

**Example prompt for the agent:**

> "List my pending tasks for project `myproj`, then claim the highest-priority approved one."

The agent will call `kanban_list(status="approved", project_id="myproj")` then `kanban_pull(task_id="T-007")`.

---

## 2. Cline (MCP)

Cline (VSCode extension, formerly Claude Dev) speaks the same MCP protocol.

**Setup.** Open VSCode → Cline panel → Settings (gear icon) → MCP Servers → Add → paste the same JSON block as for Claude Code (with absolute paths). The extension reloads automatically.

Ask Cline: *"What's in my kanban backlog?"* — it should call `kanban_list(status="backlog")`.

---

## 3. opencode (REST + OpenAPI)

[opencode](https://opencode.ai) (sst/opencode) doesn't speak MCP, but supports model-native function calling. Two integration approaches:

### 3a. Per-tool curl shells (simple)

In `opencode.toml`:

```toml
[[tools]]
name = "kanban_list"
description = "List tasks in agent-kanban (filter by status)"
command = "curl -s 'http://localhost:7777/api/board?project=default' | jq '.tasks'"

[[tools]]
name = "kanban_create"
description = "Create a kanban task. Args: title, project_id"
command = "curl -s -X POST 'http://localhost:7777/api/tasks' -H 'Content-Type: application/json' -d '{\"title\":\"$title\",\"project_id\":\"$project_id\"}'"
```

### 3b. OpenAPI import (preferred when supported)

Static OpenAPI 3.1 schema is committed at [`docs/openapi.yaml`](openapi.yaml). Point opencode at it (or its live counterpart `http://localhost:7777/openapi.json`). All endpoints become first-class tools without manual TOML.

---

## 4. Open WebUI (OpenAPI Tool Server)

[Open WebUI](https://github.com/open-webui/open-webui) supports two paths:

- **Pipelines** — Python plugin uploaded to `/pipelines`. Heavy.
- **OpenAPI Tool Server** — point WebUI at the kanban's `/openapi.json`. Lightweight, recommended.

**Setup.** Settings → Tools → Add OpenAPI Tool Server → URL: `http://localhost:7777/openapi.json` (or `http://host.docker.internal:7777/openapi.json` if WebUI is in Docker). Save → tools auto-discovered.

If WebUI runs on a different host, enable CORS:

```bash
KANBAN_CORS_ORIGINS=https://your-webui.example python -m kanban_ui
```

---

## 5. Generic function-calling LLM

For Hermes (NousResearch), Llama 3.1, Mistral, Ollama with `tools=`, vLLM with `--enable-auto-tool-choice`, etc. — anything OpenAI-SDK-compatible.

The kanban server is a plain REST service. Define tools in your client and dispatch tool_calls back via HTTP. Full working examples in [`examples/llm-tool-calling/`](../examples/llm-tool-calling/):

- `openai_sdk_demo.py` — OpenAI Python SDK against Ollama / vLLM endpoint.
- `ollama_demo.py` — direct Ollama `/api/chat` with `tools=`.
- `curl_examples.sh` — copy-pasteable for any agent or shell-driven workflow.

Minimal sketch:

```python
from openai import OpenAI
import httpx

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")  # or any OAI-compatible
KANBAN = "http://localhost:7777"

tools = [{
    "type": "function",
    "function": {
        "name": "kanban_list",
        "description": "List tasks in a project",
        "parameters": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
        },
    },
}]

def dispatch(name, args):
    if name == "kanban_list":
        return httpx.get(f"{KANBAN}/api/board", params={"project": args["project_id"]}).json()
    raise ValueError(name)

# (then a normal tool-call loop)
```

---

## REST API reference

- **Interactive Swagger UI**: `http://localhost:7777/docs`
- **Static OpenAPI 3.1 spec**: [`docs/openapi.yaml`](openapi.yaml) (947 lines, 26 KB)
- **Live JSON**: `http://localhost:7777/openapi.json`

Key endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/board?project=<id>` | board view (tasks grouped by column) |
| GET | `/api/projects` | list of projects |
| POST | `/api/projects` | create project |
| GET | `/api/tasks/{id}` | full task with history |
| POST | `/api/tasks` | create task |
| PATCH | `/api/tasks/{id}` | edit fields |
| POST | `/api/tasks/{id}/move` | drag-drop result |
| POST | `/api/tasks/{id}/comment` | comment in history |
| POST | `/api/tasks/{id}/links` | attach link |

---

## Outbound webhooks (Slack / Telegram / generic)

The kanban can fire HTTP notifications on changes. Config:
`kanban_data/webhooks.json`, hot-reloaded by mtime:

```json
{
  "webhooks": [
    {
      "name": "Slack #dev",
      "url": "https://hooks.slack.com/services/XXX/YYY/ZZZ",
      "events": ["task_created", "task_moved"],
      "format": "slack",
      "project_id": null
    },
    {
      "name": "Telegram bot → my chat",
      "url": "https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>",
      "events": ["task_moved"],
      "format": "telegram"
    },
    {
      "name": "Custom collector",
      "url": "http://localhost:8765/collect",
      "events": ["task_created", "task_moved", "task_commented", "task_updated"],
      "format": "generic"
    }
  ]
}
```

Fields:
- `events`: `task_created` / `task_moved` / `task_commented` / `task_updated`
- `format`: `slack` (`{text: "..."}`), `telegram` (`{text: "..."}`, `chat_id` in URL), `generic` (raw JSON)
- `project_id`: limit a webhook to one project; `null` = all projects
- `enabled`: defaults to `true`

Delivery is a **fire-and-forget asyncio task**: the user's main HTTP request
isn't blocked on webhook timeouts. Delivery logs (status_code, ms) live at
`/api/automation/status.webhooks.last_deliveries`.

### How to get a Slack / Telegram URL

- **Slack**: Workspace → Apps → Incoming Webhooks → Add → pick a channel → copy the URL.
- **Telegram**:
  ```bash
  curl https://api.telegram.org/bot<TOKEN>/getMe          # verify the token
  curl https://api.telegram.org/bot<TOKEN>/getUpdates     # find your chat_id
  ```
  Then the URL: `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>`.

## Capturing Claude Code sessions

If you want tasks auto-created from Claude's session notes — wire up a
Stop-hook (or a slash command) to write a summary into
`~/.claude/projects/<encoded-path>/memory/inbox/<timestamp>.md`, and set:

```bash
export KANBAN_INBOX_DIR=~/.claude/projects/-Users-you-myproj/memory/inbox
```

The inbox watcher picks the file up within 5 seconds and turns it into a card.
See `Inbox watcher` in [README.md](../README.md).

## CORS

The kanban server binds to `127.0.0.1:7777` by default — no CORS headers, only same-origin (the bundled UI) and localhost agents (Claude Code via MCP, opencode dispatching curl) can talk to it.

For **remote** agents (Open WebUI on another host, a containerized agent) opt in via env:

```bash
export KANBAN_CORS_ORIGINS="https://webui.example,https://other.example"
python -m kanban_ui
```

Comma-separated origins. `allow_methods=*`, `allow_headers=*`, `allow_credentials=false`.

If you expose the kanban server itself externally (not just CORS — actually `0.0.0.0`), add **nginx** in front with auth — there's no built-in authentication.

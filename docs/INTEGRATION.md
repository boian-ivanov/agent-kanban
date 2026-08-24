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

Restart Claude Code (or run `claude mcp list` to confirm `✓ Connected`). 24 tools become available:

| Tool | Purpose |
|---|---|
| `kanban_columns` | list of columns + ownership semantics |
| `kanban_list` | tasks with optional status / assignee / project / parent / updated-since filters (parity with `GET /api/tasks`) |
| `kanban_get` | full card with history + links + blockers |
| `kanban_context` | agent context bundle — task + ancestor chain + recent comments + children summary + shared constraints (one call) |
| `kanban_children` | direct children, summary or full cards (`include="full"` — scoping) |
| `kanban_subtree` | recursive descendant tree (epic → stories → tickets) in one call |
| `kanban_pull` | atomically claim an `approved` task → `analyst`, assignee = current agent (legacy analyst-flow alias) |
| `kanban_claim` | atomically claim `approved` → `in_progress` with `assignee=agent:<role>` (driver claim, parity with `POST /api/tasks/{id}/claim`) |
| `kanban_move` | move card to a new column |
| `kanban_comment` | append comment to history |
| `kanban_chat` / `kanban_send_chat` | read / append the persisted per-task chat (`task_chat`) |
| `kanban_run` / `kanban_register_run` | read / upsert the task_runs row (pid, model, role, control_port, tokens) |
| `kanban_stop` / `kanban_steer` | stop the live agent (blocked / approved) or inject a message into its session |
| `kanban_create` | new card (supports `parent_id` / `kind` hierarchy) |
| `kanban_link` | attach memory/file/pr/url link |
| `kanban_blockers` | set/replace inter-task blockers |
| `kanban_update` | edit title/priority/size/description/blocker |
| `kanban_projects` / `kanban_board` / `kanban_search` / `kanban_my_active` | board overview, search, and "what am I working on" queries |

**Example prompt for the agent:**

> "List my pending tasks for project `myproj`, then claim the highest-priority approved one."

The agent will call `kanban_list(status="approved", project_id="myproj")` then `kanban_pull(task_id="T-007")`.

---

## 2. Cline (MCP)

Cline (VSCode extension, formerly Claude Dev) speaks the same MCP protocol.

**Setup.** Open VSCode → Cline panel → Settings (gear icon) → MCP Servers → Add → paste the same JSON block as for Claude Code (with absolute paths). The extension reloads automatically.

Ask Cline: *"What's in my kanban backlog?"* — it should call `kanban_list(status="backlog")`.

---

## HTTP MCP transport (`http://localhost:7777/mcp`)

In addition to the stdio server in `kanban_mcp/`, the kanban also exposes a **streamable HTTP MCP endpoint** at `/mcp`, mounted by `fastapi_mcp` directly on top of the REST routes (same FastAPI app, same data, no extra process).

Use this path when your MCP client doesn't speak stdio (Cursor, recent Cline) or for ad-hoc debugging (MCP Inspector). All operations available via the stdio server are available here too — same schemas.

Quick smoke check that the endpoint is live:

```bash
curl -s -i --max-time 2 http://localhost:7777/mcp
# → HTTP/1.1 200 OK
# → content-type: text/event-stream
# → event: endpoint
# → data: /mcp/messages/?session_id=…
```

### Cursor (HTTP MCP) <a id="cursor-http-mcp"></a>

[Cursor](https://cursor.com) speaks HTTP MCP natively.

**Setup.** Open Cursor → `Cmd+,` (Settings) → search "MCP" → Add new server. Or edit `~/Library/Application Support/Cursor/User/settings.json` directly:

```jsonc
{
  "mcp": {
    "servers": {
      "agent-kanban": {
        "url": "http://localhost:7777/mcp"
      }
    }
  }
}
```

Restart Cursor; in chat ask: *"What's in my kanban backlog for project myproj?"*. Cursor will call the kanban tools via the HTTP transport and respond with live state.

### Cline (HTTP) <a id="cline-http-mcp"></a>

Recent Cline versions (≥ 3.x) support HTTP MCP servers alongside stdio.

**Setup.** VSCode → Cline panel → ⚙ Settings → MCP Servers → "Add HTTP server" → URL `http://localhost:7777/mcp`. Reload Cline.

If you've been using the old stdio Cline config (section 2 above), you can keep both — Cline merges tool listings from all servers, but the kanban will appear twice. Pick one transport per Cline instance to avoid duplicate tools in the picker.

### MCP Inspector <a id="mcp-inspector"></a>

[modelcontextprotocol/inspector](https://github.com/modelcontextprotocol/inspector) is the canonical debugging tool for MCP servers — a browser-based UI that lets you call tools by hand, inspect schemas, and watch the SSE stream live.

```bash
npx @modelcontextprotocol/inspector http://localhost:7777/mcp
# opens http://localhost:5173 in your browser
```

In the Inspector UI:

1. **Transport:** auto-detected as SSE on first connect.
2. **Tools tab:** lists all kanban operations exposed via the HTTP endpoint — click any to invoke with form inputs.
3. **Network tab:** every request/response in raw JSON-RPC, useful when something feels off.

This is the fastest way to verify a fresh install before wiring up a real client.

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

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tasks` | **filtered task list** — `project` / `status` / `assignee` / `parent_id` / `updated_since` (summary cards) |
| GET | `/api/tasks/{id}` | full task with history (`?since_seq=` for incremental comment polls) |
| GET | `/api/tasks/{id}/context` | agent context bundle (task + ancestors + comments + children + constraints) |
| GET | `/api/tasks/{id}/children?include=summary\|full` | direct children (full = complete cards) |
| GET | `/api/tasks/{id}/subtree` | recursive descendant tree (epic → stories → tickets) |
| POST | `/api/tasks` | create task (supports `parent_id` / `kind`) |
| PATCH | `/api/tasks/{id}` | edit fields |
| POST | `/api/tasks/{id}/claim` | atomic claim: `assignee=agent:<role>` + `approved` → `in_progress` |
| POST | `/api/tasks/{id}/move` | move `{to_status}` |
| POST | `/api/tasks/{id}/assign` | assign/unassign `{assignee}` |
| POST | `/api/tasks/{id}/comment` | comment `{text}` |
| POST | `/api/tasks/{id}/links` | attach memory/file/pr/url link |
| GET/POST | `/api/tasks/{id}/chat` | persisted per-task agent chat (`task_chat`) |
| GET/POST | `/api/tasks/{id}/runs` | task_runs row (pid, model, role, status, control_port, tokens) |
| POST | `/api/tasks/{id}/agent/stop` | stop the live agent (`reason`, `to_status`: blocked \| approved) |
| POST | `/api/tasks/{id}/agent/steer` | inject a message into the live agent session |
| GET | `/api/tasks/{id}/log/stream` | SSE: live agent log stream |

### Agent context protocol (epic → story → task)

The hierarchy exists so dispatched agents get the FULL context of their ticket:

- **Upward (automatic)**: the driver fetches `GET /api/tasks/{id}/context` before
  every run and injects the bundle — task fields, ancestor chain, recent
  comments, a `children` summary, shared constraints. Agents should NOT
  re-fetch context; it is already in their prompt.
- **Downward (children)**: `GET /api/tasks/{id}/children?include=full` returns
  full child cards in one call (description/acceptance/parent chain/comments).
  `GET /api/tasks/{id}/subtree` returns the complete recursive descendant tree
  with nested `children` — one call, no N+1.
- **Scoping flow (D3)**: an epic/story assigned `agent:scoping` fetches
  `/subtree` first, reviews it against the codebase, creates child
  stories/tickets (`parent_id` + description + acceptance, never
  `status:approved`), comments a summary and moves the epic/story to `uat`.

### Agent lifecycle: claim, chat, stop/steer, budgets

- **Claim**: `POST /api/tasks/{id}/claim` sets `assignee=agent:<role>` and moves
  `approved` → `in_progress` atomically (history-recorded, idempotent for the
  same assignee, 409 on ownership/status conflicts). The task driver claims
  this way at dispatch.
- **Chat**: `task_chat` is the single source of truth for the agent
  conversation — `GET /api/tasks/{id}/chat` (ascending `seq`) and
  `POST /api/tasks/{id}/chat` (`{role, content}`). Per-task by construction,
  persists after the run ends.
- **Stop/steer**: `POST /api/tasks/{id}/agent/stop` (`reason`, `to_status`
  `blocked` for human intervention or `approved` for a routine auto-retry —
  D2) SIGINTs the omp session, comments the reason and moves the task.
  `POST /api/tasks/{id}/agent/steer` injects a user message into the live
  session. Both relay through the driver's control socket (port in
  `task_runs.control_port`); 409 when no live run is registered.
- **Budgets (D5)**: the driver enforces `max_tokens` (default 30M),
  `max_duration` (default 60 min), a token-scaled no-progress watchdog and
  dot-output detection. Before any interrupt the driver re-verifies it still
  owns the run row (non-owner never touches the board).

### Verification agent (T-314)

A task arriving in `testing` fires `examples/launch-verifier.sh`; the verifier
(role `verification`) runs the repo gate, smokes the changed path, then moves
the task `done` (PASS, with evidence) or `approved` (FAIL, with findings) or
`blocked` (human intervention). Verifiers never commit (D1) — the board owner
commits after `done`. **Smoke-test cleanup is ownership-scoped**: only the
processes the verifier itself spawned are stopped (exact pid recorded at
spawn; never `pkill` by pattern or kill by port — the live board on 7777 or
the retry's process may own it).

### Ticket IDs

IDs are per-project: `{code}-{seq:03d}` (e.g. `SP-001`), where `code` is the
project's uppercase prefix (`projects.code`, set via `POST /api/projects`
or `PATCH /api/projects/{id}`). Each project's sequence starts at 001 and
is tracked in `project_seq`. Projects without a code keep the legacy global
`T-{n:03d}` ids (`meta('next_id')`); existing `T-###` tasks are never
rewritten. A project code can only be changed before its first
`{code}-###` ticket exists, and codes are unique across projects.

---

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

# agent-kanban

> Local-first kanban for AI-agent workflows. SQLite, FastAPI, no auth, no cloud.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

A self-hosted kanban board designed to be driven by AI coding agents
(Claude Code, Cline, opencode, Open WebUI, any function-calling LLM).
Drag-drop in browser, REST + MCP for agents. Multi-project, light/dark
theme, markdown plan-file import, automation rules.

![Board — light theme](docs/screenshots/board-light.png)

<details>
<summary>More themes & profiles</summary>

| Dark theme | Cyberpunk profile | Horizon profile |
|---|---|---|
| ![dark](docs/screenshots/board-dark.png) | ![cyberpunk](docs/screenshots/board-cyberpunk.png) | ![horizon](docs/screenshots/board-horizon.png) |

Toggle theme with `t`, cycle profiles with `p`. Or pin a theme/profile via URL: `?theme=light&profile=cyberpunk`.

</details>

## Features

- 9 workflow columns: Backlog → Approved → Analyst → In progress → Testing → UAT → Done, plus Blocked / Cancelled.
- Multiple projects in one DB, URL-routed (`/p/{slug}`), each with optional Claude Code-project directory binding.
- Light/dark theme switcher (`t`), density toggle (`d`), collapsible columns, sidebar.
- Search + filter chips, keyboard shortcuts (`/`, `n`, `r`, `\`, `Esc`).
- Inbox watcher: drop a `.md` into `kanban_data/inbox/`, get a card in 5 sec. Point `KANBAN_INBOX_DIR` at any folder you like — even `~/.claude/projects/<proj>/memory/inbox/` for Claude session captures.
- PLAN.md import (loose-mode parser): any `## Section` → cards in backlog with section-name in description.
- Automation rules (`kanban_data/rules.json`): `task_idle: status=done, days=14 → move_to cancelled`, etc. Hot-reloaded.
- **Outbound webhooks** (`kanban_data/webhooks.json`): POST to Slack / Telegram / any HTTP endpoint on `task_created`/`task_moved`/`task_commented`/`task_updated`. 3 formats: `generic`, `slack`, `telegram`.
- **MCP server** (`kanban_mcp/`) with 14 tools for Claude Code / Cline (project-aware: `kanban_projects`, `kanban_board`, `kanban_search`, `kanban_my_active`).
- **REST API + auto-generated OpenAPI** for opencode / Open WebUI / any LLM with function calling.

## Quickstart

**5 minutes from clone to an AI agent doing your work for you:** see [QUICKSTART.md](QUICKSTART.md).

Bare minimum (one command after install):

```bash
# Install uv once: `curl -LsSf https://astral.sh/uv/install.sh | sh` or `brew install uv`
git clone https://github.com/<your-user>/agent-kanban.git
cd agent-kanban
uv run python -m kanban_ui                     # http://localhost:7777
```

`uv run` auto-creates a `.venv/`, installs deps from `pyproject.toml` (~5 sec on a warm cache), and launches the server.

<details>
<summary>Legacy pip / venv path</summary>

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m kanban_ui                  # http://localhost:7777
```

`requirements.txt` is kept in sync with `pyproject.toml` for Dependabot and pip-only environments.

</details>

Optional — auto-start on login (macOS):

```bash
bash scripts/install_launchd.sh install        # macOS launchd
bash scripts/install_launchd.sh status
bash scripts/install_launchd.sh uninstall
```

Logs: `~/Library/Logs/agent-kanban/{stdout,stderr}.log`.

## AI agent integrations

Pick the integration matching your agent:

| Agent | Path | Reference |
|---|---|---|
| **Claude Code** (Anthropic CLI) | MCP via `~/.claude.json` or `.mcp.json` | [docs/INTEGRATION.md#claude-code](docs/INTEGRATION.md#1-claude-code-mcp) |
| **Cline** (VSCode extension) | MCP, same config block | [docs/INTEGRATION.md#cline](docs/INTEGRATION.md#2-cline-mcp) |
| **opencode** (sst/opencode) | OpenAPI tools / REST | [docs/INTEGRATION.md#opencode](docs/INTEGRATION.md#3-opencode-rest--openapi) |
| **Open WebUI** | OpenAPI Tool Server pointing at `/openapi.json` | [docs/INTEGRATION.md#open-webui](docs/INTEGRATION.md#4-open-webui-openapi-tool-server) |
| **Generic LLM** (Hermes / Llama / Ollama / Mistral / vLLM) | function-calling + REST | [docs/INTEGRATION.md#generic-llm](docs/INTEGRATION.md#5-generic-function-calling-llm) |

Static OpenAPI schema: [`docs/openapi.yaml`](docs/openapi.yaml). Interactive Swagger: `http://localhost:7777/docs`.

Real-world flows: [`docs/USECASES.md`](docs/USECASES.md) — 11 use cases (solo dev, team Slack, legacy import, multi-project, agent session, auto-launch, etc.).

## Why

- **vs. Trello/Jira/Linear** — local-first; no SaaS account, no rate limits, your data on your disk.
- **vs. plain TODO.md** — drag-drop, history, multi-project, MCP integration, automation rules.
- **vs. building your own** — already battle-tested for solo + agent workflow.

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `KANBAN_HOST` | `127.0.0.1` | bind address |
| `KANBAN_PORT` | `7777` | port |
| `KANBAN_DB` | `<repo>/tasks.db` | SQLite path |
| `KANBAN_ACTOR` | `user` | author name written to history |
| `KANBAN_DEFAULT_PROJECT_ID` | `default` | id of seed project on first launch |
| `KANBAN_DEFAULT_PROJECT_NAME` | `Default` | display name of seed project |
| `KANBAN_DEFAULT_PROJECT_COLOR` | `#F10D30` | accent color of seed project |
| `KANBAN_INBOX_DIR` | `<repo>/kanban_data/inbox` | inbox watcher folder |
| `KANBAN_RULES_FILE` | `<repo>/kanban_data/rules.json` | automation rules |
| `KANBAN_WEBHOOKS_FILE` | `<repo>/kanban_data/webhooks.json` | outbound webhook notifications |
| `KANBAN_AUTOMATION_INTERVAL` | `60` | rule engine interval (sec) |
| `KANBAN_INBOX_INTERVAL` | `5` | inbox poll interval (sec) |
| `KANBAN_CORS_ORIGINS` | (empty) | comma-separated origins for CORS (e.g. for remote Open WebUI) |
| `KANBAN_PROJECT_ID` | (empty) | for MCP server: default project_id when agent calls `kanban_create` without one |

## Project layout

```
agent-kanban/
├── kanban_store/    SQLite store + schema (4 tables, 4 migrations)
├── kanban_ui/       FastAPI web UI + automation/{inbox,rules,plan_md}
│   └── static/      index.html · styles.css · app.js · vendor/Sortable
├── kanban_mcp/      MCP server (14 tools)
├── seed/            generic example tasks
├── examples/        llm-tool-calling demo scripts
├── docs/            INTEGRATION.md, openapi.yaml, screenshots
├── tests/           pytest tests
├── scripts/         install_launchd.sh
├── snapshots/       JSON snapshots (gitignored except .gitkeep)
└── kanban_data/     runtime DB, inbox/, rules.json (gitignored)
```

## Roadmap

- Bidirectional sync: edits in UI write back to PLAN.md.
- GitHub Issues importer (already has source-config table; needs gh API client).
- Multi-user mode with simple cookie-based auth.
- Optional Postgres backend.

## License

[MIT](LICENSE)

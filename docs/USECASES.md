# Use Cases — agent-kanban

How the kanban gets used in practice. Each UC is a short scenario: who acts,
what they do, what they get back. Only what the system actually supports
today — no "future features".

## Task lifecycle

```
                user                                       agent (Claude/Cline/...)
                 ↓                                                ↓
   ┌────────┐  push  ┌──────────┐  pull  ┌──────────┐  ────→  ┌────────────┐
   │ Backlog│ ─────→ │ Approved │ ─────  │ Analyst  │         │ In progress│
   └────────┘        └──────────┘        └──────────┘         └────────────┘
                                                                    │
                                                                    ↓
   ┌────────┐  ←──── ┌──────────┐  ←──── ┌────────────┐
   │  Done  │ accept │   UAT    │  done  │  Testing   │
   └────────┘        └──────────┘        └────────────┘

   Blocked — a parallel column for anything that's stuck.
   Cancelled — terminal state for "we're not doing this".
```

Column "owners" (see `kanban_columns()`) are a **semantic hint**, not access
control. The UI and API let you move a card anywhere. Columns can be edited
in `kanban_store/store.py` (`STATUSES` + `status_meta()`).

---

## UC-0: First-time setup

**Who:** new user who just cloned the repo.

**Steps:**
1. `git clone … && cd agent-kanban`
2. `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. `.venv/bin/python -m kanban_ui` → http://localhost:7777
4. They see the **`default` project** with an empty board, 9 columns, sidebar on the left, and a `+ Task` button in the topbar.

**Outcome:** runs locally, DB in `tasks.db`, no accounts, no cloud.

Optional: `bash scripts/install_launchd.sh install` — auto-start on login.

---

## UC-1: Solo developer with Claude Code

**Who:** one person, with Claude Code open in some project.

**Trigger:** I want the agent to see my tasks and move cards as work progresses.

**Steps:**
1. Create a project in the kanban (`+ New project` → name + slug + directory = current repo).
2. Drop a `.mcp.json` at the repo root:
   ```jsonc
   { "mcpServers": { "agent-kanban": {
       "type": "stdio",
       "command": "/abs/.venv/bin/python",
       "args": ["-m", "kanban_mcp"],
       "cwd":  "/abs/agent-kanban",
       "env":  { "KANBAN_PROJECT_ID": "myproj" }
   }}}
   ```
3. Restart Claude Code (`/mcp restart agent-kanban`).
4. In chat: *"show me what I'm working on"* → the agent calls `kanban_my_active(assignee="claude", project_id="myproj")`.

**Outcome:** the agent knows the context — it can `kanban_pull` a task from Approved, drop `kanban_comment` notes as it works, and finally `kanban_move(task_id, "testing")`.

---

## UC-2: Team channel in Slack/Telegram

**Who:** a team of 2-5 people who want chat notifications when something moves.

**Steps:**
1. Get a Slack incoming webhook URL, or `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT>` for Telegram.
2. Create `kanban_data/webhooks.json`:
   ```json
   { "webhooks": [
     { "name": "Slack #dev", "url": "https://hooks.slack.com/...",
       "events": ["task_moved", "task_created"], "format": "slack" }
   ]}
   ```
3. No restarts needed — hot-reload by mtime.

**Outcome:** when `task_moved` fires, the chat gets `→ [Project] T-123 «Title»: backlog → in_progress — comment`. Delivery is fire-and-forget, the main HTTP request isn't blocked. Delivery logs at `/api/automation/status.webhooks`.

---

## UC-3: Importing existing project (legacy `PROJECT-PLAN.md`)

**Who:** a user who already has markdown plans in a repo.

**Trigger:** I want to wire those files into the board without rewriting them by hand.

**Steps:**
1. `+ New project` → ID slug + directory → click "Choose…" → native Finder.
2. Under the **"Existing plans"** tab the kanban scans files in the directory and shows candidates by priority (`PROJECT-PLAN.md`, `BACKLOG.md`, `TODO.md` first). Plan files (priority ≤ 5) are pre-checked.
3. Click "Save".

**What happens:**
- The parser reads each file and walks `## ...` sections:
  - Canonical headings (`## Backlog`, `## Done`, `## Бэклог`) → tasks land in the matching column
  - Anything else (`## 🔴 Tier 0`, `## v2 stages`) → goes into `backlog` with the section name copied into `description` for context
- Cards are created idempotently (uniqueness on `project_id + title`)
- Appends an instruction block to `CLAUDE.md` so Claude in that folder knows new tasks should be written into the plan file

**Outcome:** an Aizav2 project with 183 tasks in one second, no copy-paste.

---

## UC-4: Daily standup in 30 seconds

**Who:** you in the morning, with Claude Code open.

**Steps:**
1. *"What am I working on right now?"* → `kanban_my_active(assignee="claude")`
2. *"Anything stuck?"* → `kanban_list(status="blocked")`
3. *"What's the priority in the backlog?"* → `kanban_list(status="backlog")` + the client filters `priority=high`

**Outcome:** plan for the day, no browser needed.

UI alternative: `/p/myproj` → HIGH filter → density compact → everything visible at a glance.

---

## UC-5: Full agent session on a single task

**Who:** Claude Code session, you say "take T-008 and ship it".

**Flow:**
1. `kanban_get("T-008")` — reads description + acceptance + history.
2. `kanban_pull("T-008")` — atomically `approved → analyst, assignee=claude`.
3. *(code analysis, plan)* → `kanban_comment("T-008", "Plan: ...")`.
4. `kanban_move("T-008", "in_progress", comment="started writing the parser")`.
5. *(work)* → `kanban_link("T-008", "pr", "https://github.com/.../pull/42")`.
6. `kanban_move("T-008", "testing", comment="code+tests ready")` — webhook fires into Slack.
7. In the UI you see the card in Testing, you review → drag-drop into **UAT** → **Done**.

**Outcome:** the task ran the full workflow; history holds every step with the actor (`claude`), PR links, and comments.

---

## UC-6: Auto-archive old tasks

**Who:** user whose board is cluttered with done cards.

**Steps:** in `kanban_data/rules.json`:
```json
{ "rules": [{
  "name": "Done > 30 days → cancelled",
  "trigger": { "type": "task_idle", "status": "done", "days": 30 },
  "action":  { "type": "move_to", "status": "cancelled",
               "comment": "Auto-archive" }
}]}
```

**Outcome:** every 60 seconds (`KANBAN_AUTOMATION_INTERVAL`) the engine checks the rules and moves stale cards. All actions land in history with `actor=automation`.

Supported triggers: `task_idle` (by `moved_at`), `task_count_in_status` (gt/lt).
Supported actions: `move_to`, `add_comment`, `set_priority`.

---

## UC-7: Inbox capture (ad-hoc notes)

**Who:** I'm working and an idea pops up.

**Steps:**
- Write `~/Projects/agent-kanban/kanban_data/inbox/quick-note.md`:
  ```markdown
  ---
  title: "Pin" button for important tasks
  priority: low
  size: S
  ---
  Pins the card to the top of its column.
  ```
- Within 5 seconds the card appears in the backlog. The file is moved to `inbox/processed/2026-05-09/quick-note.md`.

You can point the watcher at any folder via `KANBAN_INBOX_DIR`. For example, `~/.claude/projects/<encoded>/memory/inbox/` — Claude drops session summaries there and the kanban picks them up.

---

## UC-8: Multiple Claude Code projects at once

**Who:** I jump between 3 projects in different directories during the day.

**Steps:**
1. Each repo gets its own `.mcp.json` with `KANBAN_PROJECT_ID="<slug-of-this-project>"`.
2. When I'm in `~/code/myapp`, Claude only sees and moves `myapp` tasks.
3. In the UI: `Cmd+Shift+R` → URL `/p/myapp` or click in the sidebar.

**Outcome:** contexts don't bleed into each other; per-project history stays isolated.

The UI sidebar remembers the last-opened project (`localStorage.kb.lastProject`) — wherever you were last is where you land.

---

## UC-9: Browser-only workflow (no AI)

**Who:** I don't use an AI agent, or I use a separate web chat.

**Steps:**
- `+ Task` or `n` to create.
- Drag-drop cards across columns.
- Mouse wheel on empty space → horizontal pan across the board.
- Hover a column → `+` for quick-add (single-line title, Enter to create).
- Click a card → modal with history, links, blockers, comments.
- Search (`/`), filter chips (HIGH / Blocker / Agents / Free).
- Density toggle (`d`) for tight mode at 100+ tasks.

**Outcome:** the kanban is no different from Trello/Jira — just local and account-free.

---

## UC-11: Auto-launch agent on Approved

**Who:** solo dev who drags a task from Backlog to Approved and wants the agent to take it from there.

**Trigger:** drag-drop in the UI, or `POST /api/tasks/{id}/move` → `to_status="approved"`.

**Setup:**

1. **Bind the kanban project to the Claude Code project directory** (UI → `⋯` → "Project directory"). That directory needs an `.mcp.json` connecting to agent-kanban (see [INTEGRATION.md](INTEGRATION.md#1-claude-code-mcp)).

2. **Wire up the bundled launcher** — `examples/agent-launcher/launch-claude.sh`. Make it executable (`chmod +x`).

3. **Add a rule in `kanban_data/rules.json`:**
   ```json
   {
     "rules": [{
       "name": "Auto-claim approved tasks",
       "enabled": true,
       "trigger": {
         "type": "task_moved",
         "to_status": "approved",
         "project_id": "myproj"
       },
       "action": {
         "type": "run_command",
         "cmd": "/abs/path/to/agent-kanban/examples/agent-launcher/launch-claude.sh",
         "args": ["{task_id}", "{project_id}"],
         "log_file": "~/Library/Logs/agent-kanban/launcher.log"
       }
     }]
   }
   ```

   Hot-reload by mtime — no restart needed.

**What happens on drag-drop into Approved:**

1. UI/API → `move_task(T-027, to_status="approved")` → DB write.
2. The endpoint emits a `task_moved` event → the rule engine matches and runs `launch-claude.sh T-027 myproj` in the background.
3. The script fetches the task description over REST, builds a prompt, and starts `claude -p "..." --permission-mode=acceptEdits` in the project directory in the background (`nohup ... &`).
4. Through MCP, Claude calls `kanban_pull(T-027)` (approved → analyst, assignee=claude), posts a plan via `kanban_comment`, moves to `in_progress`, implements, and finally `kanban_move(T-027, "testing", comment="ready for review")`.
5. In the UI you see the card in Testing, you review → drag into **UAT** → **Done**.

**Triggers (rule.trigger.type=task_moved):**
- `to_status` (required) — destination column.
- `from_status` (optional) — source column. If set, it filters.
- `project_id` (optional) — limit to one project.

**Action `run_command`:**
- `cmd` — path to an executable (on the server).
- `args` — list with placeholders: `{task_id}`, `{title}`, `{project_id}`, `{from_status}`, `{to_status}`.
- `log_file` (optional) — if set, the script's stdout/stderr are written there.

The script runs in the background (`asyncio.create_subprocess_exec`); the kanban **does not wait** for it to finish — an agent session can run for minutes or hours.

**Security note:** `cmd` and `args` execute on the server with no sandboxing. Keep the script yours, and don't pull webhook payloads from untrusted sources into it.

**Your own agent instead of Claude Code:** copy `launch-claude.sh` → `launch-myagent.sh`, replace `claude -p` with your CLI (`opencode`, `aider`, an OpenAI SDK wrapper). Same contract: fetch the task over REST, start the agent in the background.

---

## UC-10: Snapshot for reporting

**Who:** end of the week, you need to show what got done.

**Steps:**
- `Snapshot` button in the topbar, or `POST /api/snapshot`.
- `snapshots/2026-05-09.json` now holds a full JSON dump: every project + every task + history.

**Outcome:** an artifact you can commit, send, or analyze.

Snapshots are gitignored by default (see `.gitignore`); flip the rule if you want them versioned.

---

## What's **not** in the current scope

- Multi-user auth — the kanban listens on `127.0.0.1` only. If you want a team, run nginx with basic-auth or Authelia in front, or wait for the Roadmap "multi-user mode".
- Bidirectional `PLAN.md` ↔ kanban — for now it's one-way (file → kanban). UI edits are **not written back** to the file (see Roadmap).
- Sub-tasks / hierarchy — `task_blockers` covers dependencies (DAG), but there's no real nesting.
- Time tracking — `moved_at` and `created_at` exist in history; aggregate on top yourself via `/api/snapshot`.
- GitHub Issues sync — `project_source.git` stores a URL+token, but issue import isn't implemented yet.

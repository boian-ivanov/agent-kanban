---
name: kanban-tickets
description: Report bugs and feature requests for the agent-kanban repo as board tickets (AK-###). Ground each issue in the code (root cause, exact file/rule), keep cards S/M with acceptance criteria, and never create with status approved (the user or orchestrator dispatches deliberately). Use whenever the user reports board bugs, wants features, or asks to raise cards on the agent-kanban project.
---

# Kanban Tickets — Issue Intake (agent-kanban repo)

Turn user-reported bugs and feature requests into grounded cards on the
`agent-kanban` project. Example that worked: AK-004 (task modal lost padding
— user report → browser-verified → root-caused to a deleted `.modal__body`
rule in commit dd1a2bf → S card with the exact restore → dispatched after the
concurrent styles.css ticket).

## Workflow

1. **Ground the issue** — reproduce first (browser-drive the modal/UI or hit
   the REST API), then find the exact cause: `git diff`/`git log` on the
   suspect commit, the specific file/rule/line. Never file a card without
   knowing what the fix touches.
2. **Check for existing cards** — `GET /api/board?project=agent-kanban`;
   supersede or extend instead of duplicating.
3. **Create** (`POST /api/tasks`):

```json
{
  "title": "fix: <area> <verb-phrase>",
  "description": "User report + reproduction evidence + root cause (commit/rule) + fix hint\n\nTouches: kanban_ui/main.py, tests/test_project_ids.py",
  "acceptance": "Falsifiable criteria — what 'done' means, how it's checked",
  "status": "backlog", "priority": "high|normal|low", "size": "S|M",
  "project_id": "agent-kanban"
}
```

- IDs are `AK-###` (per-project numbering, T-316). Never hand-craft ids.
- S < 30 min, M < 2 h. No L cards — split.
- `priority: high` = broken/regression (like the modal); `normal` = features.
- **Never** create with `status: approved` — that auto-dispatches the work
  agent. The user or the orchestrator dispatches deliberately.
- **Every new card MUST carry a machine-readable `Touches:` line** — the last
  line of `description`: `Touches: <comma-separated paths or areas>`, e.g.
  `Touches: kanban_ui/main.py, tests/test_project_ids.py`. Use real repo paths
  or named areas. It is what makes the card **schedulable next to another one**:
  two cards run at once only if their `Touches` sets are disjoint, and a card
  whose `Touches` overlap anything in flight is serialized instead (see
  `skill://kanban-pipeline` → "Multiple tickets in flight").
- Cards that touch the same file must never run together. With worktree lanes
  on (per-card worktree on branch `task/<card_id>` — `projects.worktrees` on the
  project row, see `skill://kanban-pipeline` → "Worktree lanes") disjoint cards
  run in parallel, each in its own tree; **without lanes the worktree is SHARED
  and one agent edits at a time**, so a card touching a running card's file is
  queued AFTER it. `agent-kanban` has no lane config yet (`worktrees: null`), so
  today every such card is queued. Hot files to check first:
  `examples/task-driver.py`, `kanban_store/store.py`, `kanban_store/schema.sql`,
  migrations.
- Include the exact fix when the cause is a deleted/changed rule (the agent
  restoring it will use your diff verbatim).

4. **Keep the board consistent** — superseded cards → `cancelled` with a
   pointer comment; scope changes → PATCH the card, comment the change.
5. **Verify** — re-`GET` the board, list the affected columns.

## Gotchas

- The repo gate is `uv run pytest tests/` — acceptance should reference it,
  not `bun check` (pure Python repo).
- The verifier (T-314) auto-checks cards landing in `testing`; acceptance
  criteria should be verifiable by an agent (commands + observable state).
  Verifier cleanup is ownership-scoped (only processes it spawned — exact
  pid/port; never `pkill` by pattern or kill by port; the live board on 7777
  or the retry's process may own it — 3 incidents 2026-08-24).
- The work agent's protocol (REST + stdio MCP parity): atomic claim
  `POST /api/tasks/{id}/claim` (approved → in_progress), DB chat
  `GET/POST /api/tasks/{id}/chat`, control `POST /api/tasks/{id}/agent/stop|
  steer`, context `GET /api/tasks/{id}/context`, scoping
  `children?include=full` + `subtree`. Cards with a parent/kind are
  hierarchical (epic → story → task).
- Design decisions → Obsidian vault note first (`Agent Kanban — Redesign
  Plan.md` holds the roadmap + locked decisions D1–D6), then tickets.
- Full board protocol lives in `skill://kanban-pipeline`.

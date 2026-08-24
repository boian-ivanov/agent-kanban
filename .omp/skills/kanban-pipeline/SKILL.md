---
name: kanban-pipeline
description: Orchestrate the agent-kanban ticket pipeline for the agent-kanban repo (dogfooding the board itself). Load when asked to monitor the board, run the work loop, verify finished agent work, commit & push task results, close tasks, dispatch the next ticket, or handle agent incidents (churn, dead sessions, watchdog false positives, destructive-git). Covers the driver-based dispatch protocol, the verifier-on-testing loop, commit conventions, sequencing, and incident handling.
---

# Kanban Orchestration (agent-kanban repo)

Run the dogfood loop: dispatch tickets on the `agent-kanban` project, monitor
the driver-run agents, let the verifier check `testing` arrivals, then review,
commit & push, close, and dispatch the next card.

## Board

- Server: `http://127.0.0.1:7777` · project: `agent-kanban` (repo
  `~/Projects/agent-kanban`, board id prefix `AK-` since T-316)
- REST: `GET /api/board?project=agent-kanban`, `GET /api/tasks`
  (filtered: project/status/assignee/parent_id/updated_since),
  `GET /api/tasks/{id}` (full card, `?since_seq=` comment poll),
  `GET /api/tasks/{id}/context` (agent context bundle),
  `GET /api/tasks/{id}/children?include=summary|full`,
  `GET /api/tasks/{id}/subtree` (recursive descendant tree),
  `GET /api/tasks/{id}/chat` (persisted messages) / `POST` (add message),
  `GET /api/tasks/{id}/runs` (live run: pid, role, status, control_port,
  tokens_used) / `POST` (driver upsert),
  `POST /api/tasks/{id}/claim` (atomic: assignee=agent:<role> + approved→in_progress),
  `POST /api/tasks/{id}/comment|move`,
  `POST /api/tasks/{id}/agent/stop|steer` (control socket relay)
- Columns: backlog → approved → analyst → in_progress → testing → uat → done;
  blocked / cancelled. `approved` fires the launcher; `testing` fires the
  **verifier** (T-314).
- Pipeline: launcher (thin) → `examples/task-driver.py` (claims atomically
  via `POST /api/tasks/{id}/claim`, registers `task_runs`, runs the omp
  session, writes chat to DB (`task_chat`), exposes control socket) → work
  agent → moves to `testing` → verifier agent (role `verification`, no
  claim) gates + smokes → `done` (PASS) or `approved` (FAIL, with findings)
  or `blocked` (human intervention needed). **Agents never commit**
  (D1-lock) — the orchestrator commits & pushes.
- **Budgets (T-312, D5)**: the driver enforces `max_tokens` (default 30M),
  `max_duration` (default 60 min), a token-scaled no-progress watchdog and
  dot-output detection; before any interrupt it re-verifies run ownership
  (pid == the driver pid).

## The Loop

1. **Dispatch next** — pick by dependency order, then ascending id (AK-###).
   Current order after the core tickets: AK-002 → AK-003 → T-315 (docs pass).
   Comment (model: `opencode-go/deepseek-v4-flash`), move to `approved`.
2. **Monitor (churn-guarded, 10-min windows)**:
   `python3 examples/orchestrator.py --watch <ID> --timeout 600` (polls every
   20s; settles on testing/uat/done/blocked/cancelled). The watch treats
   `testing` as settled — after it fires, the **verifier phase** needs manual
   polling (status/chat/runs until `done`/`approved`).
   At each window expiry run the churn check: process alive
   (`pgrep -f "omp --mode rpc"` — note: NOT `--mode json`, the driver launches
   rpc-mode), log growth (`wc -c` + mtime of
   `kanban_data/agent-logs/<ID>.log`). Healthy → extend the window.
   **Silent-read stalls are NOT churn**: deepseek-v4-flash agents routinely go
   2–5 min quiet while reading/editing; only act if the log is frozen across a
   full window AND the process is dead or the tree shows no activity.
3. **Verify** (`testing` arrival, if the verifier is skipped/broken):
   diff review (focused, no ruff churn — repo never format-gated; baseline has
   ~30 pre-existing ruff violations, gate is **pytest**), gate
   `uv run pytest tests/`, migration smoke on a byte-copy when schema changes.
4. **Commit & push** — conventional commits, NO ticket prefix (repo history:
   `feat: …`, `fix: …`, `chore: …`). Stage ONLY the task's files; never
   `CLAUDE.md`/`PLAN.md` (untracked user files). Push `origin main`. Verify
   the commit landed (`git log --oneline -1`) BEFORE commenting the hash —
   do not guess hashes in comments ahead of the commit.
5. **Close** — comment verification result (PASS + what was checked + commit
   hash), move to `done`. If the verifier already moved it, confirm + commit
   the tree (the verifier leaves it modified).
6. **Restart the server** after commits that change server code:
   `hub restart akanban` (ready: log "Application startup complete", port
   7777). The live DB migrates at restart (default DB = repo root `tasks.db`,
   NOT `kanban_data/`).

## Gate & conventions

- Gate: `uv run pytest tests/` MUST exit 0. `ruff check` is NOT the gate (large
  pre-existing baseline); agents must add no NEW violations. `bun` never
  applies (pure Python).
- Verify commits landed before closing; never close on a failed commit.
- Server runs under `hub` name `akanban` (cwd repo root, env
  `KANBAN_ACTOR=omp KANBAN_AUTOMATION_INTERVAL=2`).

## Sequencing

- One agent at a time — the worktree is SHARED and persistent between runs;
  concurrent agents corrupt each other's edits. Wait for a ticket to reach
  `testing` (verifier phase included) before dispatching the next.
- Dependency order: tickets that extend the same file (e.g. driver) must run
  sequentially; AK-002 before AK-003 (both touch `task-driver.py`).
- `approved` = dispatch trigger — only the user or the orchestrator decides.

## Incident handling (all observed 2026-08-24)

- **Dead agent mid-run**: no process, log frozen, no move → comment + restart
  via `backlog → approved`; the fresh agent picks up the tree state. If the
  tree verifies green, commit it before restarting (dead-agent work is often
  complete).
- **Destructive git (T-312)**: agent's restore wiped its own work + user
  files. Constraint forbids `git clean/reset --hard/restore ./rm`. Recovery:
  files move to `~/.Trash` (sandbox can't read Trash — user restores via
  Finder) or regenerate via project source setup. On wipe: bounce the ticket,
  never salvage-guess.
- **Chain-test interference (T-313)**: agents spawning the launcher/driver for
  their OWN live task corrupt the run registry + log and kill the session.
  Constraint requires scratch server (temp port) + scratch task id. On
  detection: kill orphan drivers, restore the run row
  (`POST /api/tasks/{id}/runs` upsert with the real pid), steer the session.
- **Watchdog false positives (AK-003 pending)**: a second driver for the same
  task fires "Budget breach (no_progress) alive=false" on its own unspawned
  proc and tries to bounce the task. Verify ownership (`run.pid == the driver
  pid`) before trusting breach comments; restore the run row to the real pid.
- **Steering**: `@agent <text>` comments inject into the live session via the
  control socket (rule `task_commented` + prefix `@agent` + `agent_steer`).
  Use it to correct agent misbeliefs mid-run (e.g. "you are the only live
  session — continue").
- **Run row staleness**: a dead driver can leave `status=running`. Fix via the
  `POST /api/tasks/{id}/runs` upsert (`{"status":"failed","ended_at":…}`).
- **Verifier cleanup kills retry (3x, 2026-08-24)**: the verifier's
  smoke-test cleanup killed the retry's process — cleanup was not
  ownership-scoped. Rule (agents.json constraint + verification prompt):
  only stop processes YOU spawned — record the exact pid at spawn and the
  exact port you bound; never `pkill` by pattern or kill by port alone
  (`lsof -ti :PORT` can match a different owner); never touch the live board
  on 7777 or another task's driver/omp session. On detection: check
  `task_runs`/`ps` for the surviving pid and restart the retry.
- **Edit-tool mangling**: agents repeatedly corrupt files with fuzzy edits
  (deleted rules, clobbered function tails, ASCII `+` vs `＋`). They
  self-repair; verify with the gate + focused diff review. If an agent's edit
  deleted a needed rule (e.g. AK-004 `.modal__body`), the ticket must carry
  the exact restore.

## Reference

- Vault: `Agent Kanban — Redesign Plan.md`, `Agent Kanban — Local LLM Workflow
  Board.md` (root); skills live in the SALON repo too
## Agent context protocol (epic → story → task)

The hierarchy exists so agents get the FULL context of their ticket. Flows:

- **Upward (automatic)**: the driver fetches `GET /api/tasks/{id}/context`
  before every run and injects the bundle — task fields, ancestor chain,
  recent comments, a `children` summary (id/title/status/size — the story's
  planned tickets), constraints. Older boards: 404 fallback to plain task
  fetch. Agents should NOT re-fetch context; it is already in their prompt.
- **Downward (children)**: `GET /api/tasks/{id}/children?include=full` —
  full child cards in one call (description/acceptance/parent chain/comments;
  a story's tickets, an epic's stories). Summary default (`include=summary`)
  matches the `/api/tasks` card shape.
  `GET /api/tasks/{id}/subtree` — the complete recursive descendant tree
  (epic → stories → tickets) with full fields and nested `children`, one
  call, no N+1. Sibling order follows (status, column_order, id).
- **Scoping flow (D3)**: an epic/story assigned `agent:scoping` is dispatched
  on `approved`; the scoping agent first fetches the whole descendant tree
  with `GET /api/tasks/{id}/subtree` (descriptions + acceptance of every
  child in one call), reviews it against the codebase, creates child
  stories/tickets (`parent_id` + description + acceptance, S/M only, never
  `status:approved`), comments a summary, moves the epic/story to `uat` for
  user review. Analysis-only — no code changes.
- **For monitors/orchestrators**: `/context` is also the one-call way to see
  a ticket's full picture (acceptance + parent plan) before dispatching.
  (`~/Projects/salon-platform/.omp/skills/kanban-pipeline`,
  `kanban-tickets`) — that pipeline's gate/commit rules are salon-specific
  (bun check, `type(T-0XX):` lefthook), do NOT copy them here.
- Sibling skill: `skill://kanban-tickets` — issue intake for this repo.

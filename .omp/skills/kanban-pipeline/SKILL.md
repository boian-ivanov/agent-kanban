---
name: kanban-pipeline
description: Orchestrate the agent-kanban ticket pipeline for the agent-kanban repo (dogfooding the board itself). Load when asked to monitor the board, run the work loop, verify finished agent work, commit & push task results, close tasks, dispatch the next ticket, run several cards in parallel lanes, or handle agent incidents (churn, dead sessions, watchdog false positives, destructive-git). Covers the driver-based dispatch protocol, worktree lanes, multi-card concurrency, the verifier-on-testing loop, commit conventions, sequencing, and incident handling.
---

# Kanban Orchestration (agent-kanban repo)

Run the dogfood loop: dispatch tickets on the `agent-kanban` project, monitor
the driver-run agents, let the verifier check `testing` arrivals, then review,
commit & push, close, and dispatch the next card — one at a time, or a disjoint
batch when the project has lanes (see "Multiple tickets in flight").

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

## Worktree lanes (one card = one worktree)

Implemented in `examples/task-driver.py` (its module docstring is the
reference); opt-in per project, the same plumbing the salon pipeline uses.

- **Identity**: branch `task/<task_id>` (`AK-003`) in a numbered dir under the
  lane root — `<project.path>-lanes/<n>` by default. `git worktree list
  --porcelain` is the only ledger; there is no registry file.
- **Lifecycle**: the lane outlives the agent — implement → verify → commit →
  merge → release all happen in it. A re-dispatched card (budget breach, dead
  agent, verifier FAIL → `approved`) resumes its existing lane; never recycle
  or remove a lane by hand.
- **Verify runs in the implementer's lane**: `--mode verify` resolves
  `task/<task_id>` but never creates one — a verifier must not produce the tree
  it grades. No lane at verify time (pre-lane card, config added later) ⇒ it
  grades the shared tree.
- **Config is opt-in**: `projects.worktrees` on the board project row
  (`{enabled, root, base_branch, count, setup[]}`; `PATCH /api/projects/{id}`),
  with `kanban_data/worktrees.json` (this repo, gitignored) as the fallback for
  a board whose API predates the field. **No config / `enabled: false` ⇒ the
  shared tree at `project.path`, i.e. the pre-lane behaviour — which is where
  `agent-kanban` itself is today (`worktrees: null`, 2026-09-13).**
- **Dispatch failures are loud, not silent**: no free lane (`count` at
  `examples/task-driver.py:543`, allocation under an `fcntl.flock`, `:553`) or
  a failing `setup[]` command aborts the run with a card comment and leaves the
  card in `approved`; the driver never falls back to the shared tree. Retry
  when a lane is free.
- **Lane setup** (declared in `setup[]`, run once per created lane): `uv sync`
  — an uninstalled lane fails `pytest` with import errors that read like
  product bugs.

## Multiple tickets in flight (lane concurrency)

Lanes make N cards at once possible. **You are the scheduler**: there is no
dispatcher daemon, no automatic WIP cap, no automatic conflict-domain check and
no per-lane DB automation — you read the board and the cards.

1. **Prerequisite: lane mode on with `count >= 2`.** Verify before promising a
   batch: `GET /api/projects` → the project row's `worktrees.count`, or
   `examples/lane-release.sh <project> --check` (lane state: which lanes exist,
   which are dirty, which are unmerged). **`agent-kanban` has no lane config
   (`worktrees: null`), so this project is strictly one card at a time today** —
   without lanes, NEVER run two cards: no config ⇒ one shared `project.path`
   tree, and two agents in one tree is what lanes exist to prevent.
2. **WIP cap = `min(count, 3)`.** Measured 2026-09-13 on this machine: one lane
   at full tilt ≈ **1.7M tokens/min** at peak and load **~8.5 of 11 cores**, and
   the repo gate is the load spike — past ~3 concurrent gates, load-induced
   failures stop being distinguishable from real ones. `count` is a lane count,
   not a load budget. When several gates run at once, stagger them; `pytest` is
   single-process here, so there is no worker cap to set — do not oversubscribe
   the box.
3. **Choosing a batch — the conflict-domain rule (you enforce it by hand).**
   Read each candidate's **`Touches:`** line (see `skill://kanban-tickets`);
   two cards may run together **only if their `Touches` sets are disjoint**.
   Serialize — never co-schedule — any card touching the same store/component
   file as another card in flight, and in particular a second card touching
   `examples/task-driver.py`, `kanban_store/store.py`, `kanban_store/schema.sql`
   or the migration path. Dependency order still beats parallelism: a card whose
   contract a sibling consumes goes first, and the consumer is dispatched only
   after the producer is merged (a lane is cut from the committed base branch).
4. **Dispatch**: comment on each chosen card which siblings run alongside it,
   then move each to `approved` — every card gets **its own driver and its own
   lane** (`task/<card_id>`). `in_progress` holding several cards at once is
   then the expected state, not a stuck board.
5. **Monitoring N runs — per card.** Per card: `GET /api/tasks/{id}/runs` → the
   `pid` is alive and `worktree` names the lane that run used, plus the card's
   log bytes (`kanban_data/agent-logs/<ID>.log`) over each 10-minute window.
   The churn / dead-session / run-row rules in **Incident handling** apply
   **per card** — a dead run bounces only that card, and a clean lane says
   nothing about its siblings.
6. **Verification is unchanged, and per card.** The verifier fires on that
   card's `in_progress → testing` and gates + smokes inside that card's lane; it
   never creates a lane.
7. **The serial merge queue is the ONE serialization point.** Process verified
   cards strictly one at a time — **never merge two lanes concurrently**:
   gate (`uv run pytest tests/`) + commit **inside the lane** → `git merge
   --no-ff task/<card_id>` in the project tree → `git push origin main` →
   `examples/lane-release.sh <project> <card_id>` → comment the hash, close the
   card, refill that lane. `orchestrator.py --close` performs exactly this and
   is lane-aware (`examples/orchestrator.py:26-48`, `merge_lane` at `:263`). A
   merge conflict **stops the queue**: the lane stays intact, the unmerged files
   are printed and the command exits non-zero — resolve it (or send the card
   back to `approved` for a fix run in its lane) and **never release that
   lane**.
8. **Refill discipline.** Keep the pool full up to the cap from the ordered
   backlog (dependency order, then ascending id). A finished card frees a lane;
   it does not raise the cap.

## Sequencing

- **Without lanes: one agent at a time** — the worktree is SHARED and persistent
  between runs; concurrent agents corrupt each other's edits. Wait for a ticket
  to reach `testing` (verifier phase included) before dispatching the next.
  `agent-kanban` has no lane config today, so this is the rule in force here;
  with lanes on, disjoint cards in separate lanes may run together (see
  "Multiple tickets in flight").
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

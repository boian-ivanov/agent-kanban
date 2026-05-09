# Agent launcher

Scripts the kanban invokes via a `run_command` action when a card moves into
a given status. The goal: **"drag a task into Approved → the agent picks it
up itself and drives it to Testing"**.

## What's in here

| File | What it does |
|---|---|
| [`launch-claude.sh`](launch-claude.sh) | Starts Claude Code (`claude -p`) in headless mode inside the Claude Code project directory. The prompt carries the task description + acceptance + workflow instructions. The agent then drives the task analyst → in_progress → testing through MCP. |

## How to wire it up

1. **Bind the kanban project to the Claude Code project**:
   - In the kanban UI → `⋯` next to the project → set `Project directory`
     (e.g. `~/code/myapp`).
   - That directory must contain an `.mcp.json` connecting to agent-kanban
     (see [docs/INTEGRATION.md](../../docs/INTEGRATION.md#1-claude-code-mcp)).
   - Set `env: { "KANBAN_PROJECT_ID": "myproj" }` in `.mcp.json` so that
     `kanban_create` without arguments lands in your project.

2. **Add a rule in `kanban_data/rules.json`**:
   ```json
   {
     "rules": [
       {
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
       }
     ]
   }
   ```

   Hot-reloaded by mtime — no restart required.

3. **Test**: create a task in the backlog → drag it into `Approved` → within
   a second the agent should claim it (you'll see `kanban_pull` in the
   history and the status flip to `analyst`).

## Supported placeholders

These are substituted into `args` from the `task_moved` event context:

| Placeholder | Contains |
|---|---|
| `{task_id}` | task ID (T-XXX) |
| `{title}` | task title |
| `{project_id}` | project slug |
| `{from_status}` | column the card came from |
| `{to_status}` | column the card moved to |

## Security

The script runs on the kanban server (= localhost). There's no sandboxing —
`cmd` and `args` execute as-is. Don't put anything in `rules.json` you
wouldn't run from your own shell.

## Your own launcher

You can replace `launch-claude.sh` with anything else:
- `launch-opencode.sh` — for opencode (`opencode --task ...`)
- `launch-aider.sh` — for Aider
- `launch-prompt.sh` — your own wrapper around a different LLM

The script just needs to:
1. Not block the caller. Run the agent in the background (`nohup ... &`).
2. Exit fast (≤ 1 sec). The kanban does not wait for your agent — that's a long-running task.
3. Log somewhere (optional) — the agent runs on its own, with no UI.

## Concurrency

If 3 tasks flip to Approved in a row, the kanban fires 3 launchers in
parallel. If you want a cap on concurrent sessions, build a
semaphore/lockfile into the script itself:

```bash
# At the top of launch-claude.sh:
LOCK="/tmp/agent-kanban-launcher.lock"
exec 200>"$LOCK"
flock -n 200 || { echo "another launcher running, queueing ${TASK_ID}" >&2; exit 0; }
```

Or wrap a queue around it.

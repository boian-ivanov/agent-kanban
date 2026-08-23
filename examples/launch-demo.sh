#!/bin/bash
# Live launcher: dispatches a full Oh My Pi work agent for a kanban task.
# Invoked by rules.json run_command with {task_id} and {project_id}.
# The agent role is read from the task's assignee ("agent:fe", "agent:be",
# "agent:verification", "agent:investigation", ...) and resolved via
# examples/agents.json (role prompt, worktree, model, constraints).
# No assignee / unknown role -> "default". The omp agent inherits the whole
# stack: Hindsight memory (auto-recall), Obsidian MCP, kanban REST, all tools.
TASK_ID="$1"; PROJECT_ID="$2"
AGENTS_JSON="/Users/boian.ivanov/Projects/agent-kanban/examples/agents.json"
LOG="$HOME/Library/Logs/agent-kanban/launcher.log"
mkdir -p "$(dirname "$LOG")"
# Per-task agent log — the board SSE endpoint streams this.
AGENT_LOG_DIR="/Users/boian.ivanov/Projects/agent-kanban/kanban_data/agent-logs"
mkdir -p "$AGENT_LOG_DIR"
AGENT_LOG="$AGENT_LOG_DIR/$TASK_ID.log"

TASK_JSON=$(curl -s "http://127.0.0.1:7777/api/tasks/$TASK_ID")
ASSIGNEE=$(echo "$TASK_JSON" | jq -r '.assignee // ""')
if [[ "$ASSIGNEE" == agent:* ]]; then ROLE="${ASSIGNEE#agent:}"; else ROLE="default"; fi

MODEL=$(jq -r --arg r "$ROLE" '(.[$r] // .default).model // "opencode-go/deepseek-v4-flash"' "$AGENTS_JSON")
WORKTREE=$(jq -r --arg r "$ROLE" '(.[$r] // .default).worktree // "/Users/boian.ivanov/Projects/nameri.me"' "$AGENTS_JSON")
# Per-project overrides: fetch board info once for path and model.
PROJECT_INFO=$(curl -s "http://127.0.0.1:7777/api/board?project=$PROJECT_ID")
PROJECT_PATH=$(echo "$PROJECT_INFO" | jq -r '.project.path // ""')
if [[ -n "$PROJECT_PATH" && "$PROJECT_PATH" != "null" ]]; then WORKTREE="$PROJECT_PATH"; fi
PROJECT_MODEL=$(echo "$PROJECT_INFO" | jq -r '.project.model // ""')
if [[ -n "$PROJECT_MODEL" && "$PROJECT_MODEL" != "null" ]]; then MODEL="$PROJECT_MODEL"; fi
ROLE_PROMPT=$(jq -r --arg r "$ROLE" '(.[$r] // .default).prompt // ""' "$AGENTS_JSON")
CONSTRAINTS=$(jq -r '.constraints // [] | map("- " + .) | join("\n")' "$AGENTS_JSON")
mkdir -p "$WORKTREE"

PROMPT="You are a kanban worker agent dispatched by the local agent-kanban board (project $PROJECT_ID). Task: $TASK_ID (role: $ROLE).

$ROLE_PROMPT

Constraints:
$CONSTRAINTS

Board protocol:
1. Read the task (title, description, acceptance): curl -s http://127.0.0.1:7777/api/tasks/$TASK_ID
2. Mark it in progress: curl -s -X POST http://127.0.0.1:7777/api/tasks/$TASK_ID/move -H 'Content-Type: application/json' -d '{\"to_status\":\"in_progress\"}'
3. Do the work in the current directory.
4. Post a summary comment: curl -s -X POST http://127.0.0.1:7777/api/tasks/$TASK_ID/comment -H 'Content-Type: application/json' -d '{\"text\":\"<summary>\"}'
5. Move the task to testing: curl -s -X POST http://127.0.0.1:7777/api/tasks/$TASK_ID/move -H 'Content-Type: application/json' -d '{\"to_status\":\"testing\"}'

The task content defines the actual work; the API calls above are the board protocol. Reply with a brief summary of what you did."

export AGENT_LOG

cd "$WORKTREE" || exit 1
{
  echo "[$(date -u +%FT%TZ)] omp dispatch for $TASK_ID ($PROJECT_ID) role=$ROLE model=$MODEL worktree=$WORKTREE"
  # Use --mode json which streams text_delta events as the agent generates output.
  # Python extracts text deltas and writes to the per-task log (line-buffered).
  omp --mode json --no-session --model "$MODEL" "$PROMPT" 2>/dev/null \
    | python3 -u -c '
import json, os, sys

log_path = os.environ["AGENT_LOG"]
log = open(log_path, "a", encoding="utf-8")

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        continue
    t = obj.get("type")
    if t == "message_update":
        ev = obj.get("assistantMessageEvent") or {}
        if ev.get("type") == "text_delta":
            d = ev.get("delta", "") or ""
            if d:
                log.write(d)
                log.flush()
                sys.stdout.write(d)
                sys.stdout.flush()
    elif t == "agent_end":
        log.write("\n")
        log.flush()
        sys.stdout.write("\n")
        sys.stdout.flush()
        # drain remaining lines -> prevents EPIPE on omp
'
  echo "[$(date -u +%FT%TZ)] run finished for $TASK_ID"
} >>"$LOG" 2>&1 &
exit 0

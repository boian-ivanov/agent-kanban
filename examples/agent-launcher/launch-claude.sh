#!/usr/bin/env bash
# launch-claude.sh — example agent launcher for agent-kanban.
#
# Invoked by the rule engine when a task is moved into a chosen status.
# Example config snippet in kanban_data/rules.json:
#
#   {
#     "name": "Auto-claim approved tasks",
#     "enabled": true,
#     "trigger": {
#       "type": "task_moved",
#       "to_status": "approved",
#       "project_id": "myproj"
#     },
#     "action": {
#       "type": "run_command",
#       "cmd": "/abs/path/to/agent-kanban/examples/agent-launcher/launch-claude.sh",
#       "args": ["{task_id}", "{project_id}"],
#       "log_file": "~/Library/Logs/agent-kanban/launcher.log"
#     }
#   }
#
# Arguments:
#   $1 — task_id (e.g. T-027)
#   $2 — project_id (e.g. myproj)
#
# What it does:
#   1) Builds a Claude Code prompt from the task data (via the kanban REST API).
#   2) Launches headless Claude (`claude -p`) in the background inside the project directory.
#   3) Claude uses the agent-kanban MCP server (kanban_pull -> kanban_move) to
#      drive the task through analyst -> in_progress -> testing on its own.
#
# IMPORTANT:
#   - The script must launch the agent inside the **Claude Code project
#     directory** (where .mcp.json with the agent-kanban entry lives).
#   - The kanban itself is only a context source via REST in this script.

set -euo pipefail

# A launchd process has a narrow PATH; widen it so `claude`, `node`,
# `nvm shims`, etc. are reachable. Tweak for your install.
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

TASK_ID="${1:?usage: launch-claude.sh <task_id> [project_id]}"
PROJECT_ID="${2:-}"
KANBAN_URL="${KANBAN_URL:-http://localhost:7777}"

# Resolve `claude`. Order:
#   1) env CLAUDE_BIN (explicit override)
#   2) `command -v claude` on PATH
#   3) Latest version under ~/Library/Application Support/Claude/claude-code/*/
#      (on macOS Claude Code lives there; the ~/.local/bin/claude symlink
#      can go stale after updates, so we fall back to the freshest build).
_resolve_claude() {
    [[ -n "${CLAUDE_BIN:-}" ]] && { echo "$CLAUDE_BIN"; return; }
    local p
    p="$(command -v claude 2>/dev/null || true)"
    [[ -n "$p" && -x "$p" ]] && { echo "$p"; return; }
    local mac_root="$HOME/Library/Application Support/Claude/claude-code"
    if [[ -d "$mac_root" ]]; then
        # newest version by mtime
        local latest
        latest="$(/bin/ls -1t "$mac_root" 2>/dev/null | head -1)"
        if [[ -n "$latest" ]]; then
            local cand="$mac_root/$latest/claude.app/Contents/MacOS/claude"
            [[ -x "$cand" ]] && { echo "$cand"; return; }
        fi
    fi
    echo ""
}
CLAUDE_BIN="$(_resolve_claude)"
if [[ -z "$CLAUDE_BIN" ]]; then
    echo "ERROR: 'claude' not found." >&2
    echo "  Tried: PATH=$PATH" >&2
    echo "  Tried: ~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude" >&2
    echo "  Set CLAUDE_BIN env var to absolute path." >&2
    exit 1
fi

# 1) Fetch the full task card (title + description + acceptance + links)
TASK_JSON="$(curl -fsS "${KANBAN_URL}/api/tasks/${TASK_ID}")"

TITLE="$(echo "$TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("title",""))')"
DESC="$(echo "$TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("description",""))')"
ACCEPTANCE="$(echo "$TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("acceptance",""))')"

# 2) Find the project directory (project.path)
if [[ -n "$PROJECT_ID" ]]; then
    PROJ_PATH="$(curl -fsS "${KANBAN_URL}/api/projects" \
        | python3 -c "import json,sys; ps=json.load(sys.stdin)['projects']; \
             m=[p for p in ps if p['id']=='${PROJECT_ID}']; \
             print(m[0].get('path','') if m else '')")"
    if [[ -z "$PROJ_PATH" ]]; then
        echo "ERROR: project '${PROJECT_ID}' has no path; set it in UI first" >&2
        exit 1
    fi
else
    # If project_id was not passed in, fall back to the current directory.
    PROJ_PATH="$PWD"
fi

if [[ ! -d "$PROJ_PATH" ]]; then
    echo "ERROR: project path does not exist: $PROJ_PATH" >&2
    exit 1
fi

# 3) Build the agent prompt
PROMPT=$(cat <<EOF
You're connected to agent-kanban via MCP. Take task ${TASK_ID} and complete it.

Workflow:
  1. kanban_pull(task_id="${TASK_ID}") — claim it (approved -> analyst).
  2. Read the description below, plan it, write the plan as a comment
     via kanban_comment.
  3. kanban_move(task_id="${TASK_ID}", to_status="in_progress")
  4. Implement it. Along the way: kanban_comment for progress,
     kanban_link for PRs/files.
  5. kanban_move(task_id="${TASK_ID}", to_status="testing", comment="ready for review")
  6. Stop. Do not move into "uat" / "done" — a human decides that.

=== Task context ===
ID:          ${TASK_ID}
Title:       ${TITLE}

Description:
${DESC}

Acceptance criteria:
${ACCEPTANCE}
=== End of context ===

If the task is ambiguous, add a kanban_comment with clarifying questions
and move the task to "blocked". Do not try to guess.
EOF
)

# 4) Launch Claude Code headless in the project directory.
# The `-p`/`--print` flag means "run and exit" (no interactive REPL).
# `--permission-mode=acceptEdits` — for automation (no manual confirms).
# Redirect stdout/stderr to a log so progress is visible.

LOG_DIR="${HOME}/Library/Logs/agent-kanban"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/launcher-${TASK_ID}-$(date +%Y%m%d-%H%M%S).log"

echo "[$(date -u +%FT%TZ)] launch ${TASK_ID} in ${PROJ_PATH} (log: ${LOG_FILE})" >&2

# MCP server alias from the user's .mcp.json. Default is "agent-kanban",
# but yours could be "finops-kanban" / "kanban" / anything.
MCP_ALIAS="${KANBAN_MCP_ALIAS:-agent-kanban}"

# Allow-list of tools in headless mode. Without --allowedTools Claude sees
# the MCP entries but refuses to call them with "tool not allowed".
ALLOWED_TOOLS="\
mcp__${MCP_ALIAS}__kanban_columns \
mcp__${MCP_ALIAS}__kanban_get \
mcp__${MCP_ALIAS}__kanban_pull \
mcp__${MCP_ALIAS}__kanban_move \
mcp__${MCP_ALIAS}__kanban_comment \
mcp__${MCP_ALIAS}__kanban_link \
mcp__${MCP_ALIAS}__kanban_create \
mcp__${MCP_ALIAS}__kanban_update \
mcp__${MCP_ALIAS}__kanban_search \
mcp__${MCP_ALIAS}__kanban_my_active \
mcp__${MCP_ALIAS}__kanban_board \
mcp__${MCP_ALIAS}__kanban_blockers \
Bash Read Edit Write Grep Glob TodoWrite"

cd "$PROJ_PATH"
nohup "$CLAUDE_BIN" -p "$PROMPT" \
    --permission-mode=acceptEdits \
    --allowedTools $ALLOWED_TOOLS \
    > "$LOG_FILE" 2>&1 &

CLAUDE_PID=$!
echo "[$(date -u +%FT%TZ)] claude pid=${CLAUDE_PID} for ${TASK_ID}" >&2
disown
exit 0

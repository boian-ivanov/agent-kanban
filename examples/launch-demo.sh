#!/bin/bash
# Thin spawner: delegates the whole per-task agent run to the Python driver
# (examples/task-driver.py). Invoked by rules.json run_command with {task_id}
# and {project_id}; the driver resolves the role/model/worktree, claims the
# task (assignee=agent:<role>, approved -> in_progress), registers the
# task_runs row, runs the omp session, writes assistant messages to task_chat
# (per message) and archives the raw token stream to
# kanban_data/agent-logs/{task_id}.log, then marks the run done/failed.
TASK_ID="$1"; PROJECT_ID="$2"
LOG="$HOME/Library/Logs/agent-kanban/launcher.log"
mkdir -p "$(dirname "$LOG")"
DRIVER="/Users/boian.ivanov/Projects/agent-kanban/examples/task-driver.py"
  python3 "$DRIVER" --task-id "$TASK_ID" --project-id "$PROJECT_ID" \
    --watchdog-window 1800 --watchdog-min-growth 128
} >>"$LOG" 2>&1 &
  python3 "$DRIVER" --task-id "$TASK_ID" --project-id "$PROJECT_ID"
} >>"$LOG" 2>&1 &
exit 0

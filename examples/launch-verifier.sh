#!/bin/bash
# Thin spawner: dispatches the VERIFICATION agent (agents.json 'verification'
# role) when a task arrives in testing. Same driver chain as launch-demo.sh,
# but with --mode verify: no claim (the task stays in testing while the
# verifier works), the role is fixed to verification, and before registering
# its run the driver waits for any live implementer run to clear (guard:
# never dispatch while an agent process still owns the task; bounded by
# --guard-timeout, then skip with a comment). The verifier agent reads the
# acceptance criteria, runs the repo gate (salon-platform: bun run format +
# bun check; project-agnostic fallback), smokes where feasible, then:
#   PASS -> comments evidence + moves to done
#   FAIL -> comments exact findings + moves to approved (auto-retriggers the
#           fix run)
# Verifier NEVER commits (D1-lock); the board owner commits after done.
# Invoked by rules.json run_command with {task_id} {project_id}.
TASK_ID="$1"; PROJECT_ID="$2"
LOG="$HOME/Library/Logs/agent-kanban/launcher.log"
mkdir -p "$(dirname "$LOG")"
DRIVER="/Users/boian.ivanov/Projects/agent-kanban/examples/task-driver.py"
{
  echo "[$(date -u +%FT%TZ)] spawning verifier for $TASK_ID ($PROJECT_ID)"
  python3 "$DRIVER" --task-id "$TASK_ID" --project-id "$PROJECT_ID" --mode verify
} >>"$LOG" 2>&1 &
exit 0

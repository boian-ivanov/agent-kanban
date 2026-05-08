#!/usr/bin/env bash
# Copy-pasteable curl snippets for every commonly-used endpoint.
# Useful for shell-driven agents (opencode TOML), smoke-tests, manual debugging.

set -euo pipefail
KANBAN="${KANBAN_URL:-http://localhost:7777}"
PROJECT="${PROJECT:-default}"

echo "=== List projects ==="
curl -sS "${KANBAN}/api/projects" | jq '.projects[] | {id, name, total_tasks}'

echo
echo "=== Board for project '${PROJECT}' ==="
curl -sS "${KANBAN}/api/board?project=${PROJECT}" \
    | jq '.tasks | to_entries | map({col: .key, count: (.value|length)})'

echo
echo "=== Create a task ==="
curl -sS -X POST "${KANBAN}/api/tasks" \
    -H 'Content-Type: application/json' \
    -d "{\"title\":\"Created via curl\", \"project_id\":\"${PROJECT}\", \"priority\":\"normal\", \"size\":\"S\"}" \
    | jq '{id, title, status}'

echo
echo "=== Get one task (use id from previous output) ==="
LAST_ID=$(curl -sS "${KANBAN}/api/board?project=${PROJECT}" | jq -r '.tasks.backlog[-1].id // empty')
if [[ -n "${LAST_ID}" ]]; then
    curl -sS "${KANBAN}/api/tasks/${LAST_ID}" | jq '{id, title, history: .history[-3:]}'
fi

echo
echo "=== Move task to in_progress ==="
if [[ -n "${LAST_ID}" ]]; then
    curl -sS -X POST "${KANBAN}/api/tasks/${LAST_ID}/move" \
        -H 'Content-Type: application/json' \
        -d '{"to_status":"in_progress","comment":"started via curl"}' \
        | jq '{id, status}'
fi

echo
echo "=== Add a comment ==="
if [[ -n "${LAST_ID}" ]]; then
    curl -sS -X POST "${KANBAN}/api/tasks/${LAST_ID}/comment" \
        -H 'Content-Type: application/json' \
        -d '{"text":"manual check from curl"}'
fi

echo
echo "=== Snapshot board (writes JSON to snapshots/) ==="
curl -sS -X POST "${KANBAN}/api/snapshot" | jq

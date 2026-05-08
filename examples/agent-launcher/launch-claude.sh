#!/usr/bin/env bash
# launch-claude.sh — пример агент-лаунчера для agent-kanban.
#
# Вызывается из rule engine при перемещении задачи в выбранный статус.
# Пример конфига в kanban_data/rules.json:
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
# Аргументы:
#   $1 — task_id (например T-027)
#   $2 — project_id (например myproj)
#
# Что делает:
#   1) Готовит prompt для Claude Code из task data (через REST API канбана).
#   2) Запускает headless Claude (`claude -p`) в фоне в директории проекта.
#   3) Claude через MCP-сервер агент-канбана (kanban_pull → kanban_move) сам
#      проводит задачу через статусы analyst → in_progress → testing.
#
# ВАЖНО:
#   - Скрипт должен запускать агента в **директории Claude Code-проекта**
#     (там лежит .mcp.json с подключением к agent-kanban).
#   - Сам канбан в этом скрипте — только источник контекста через REST.

set -euo pipefail

# launchd-процесс имеет узкий PATH; расширяем чтобы найти `claude`,
# `node`, `nvm shims`, etc. Меняй под свою установку.
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

TASK_ID="${1:?usage: launch-claude.sh <task_id> [project_id]}"
PROJECT_ID="${2:-}"
KANBAN_URL="${KANBAN_URL:-http://localhost:7777}"

# Резолвим claude. Порядок:
#   1) env CLAUDE_BIN (явное переопределение)
#   2) `command -v claude` в PATH
#   3) latest версия из ~/Library/Application Support/Claude/claude-code/*/
#      (на macOS Claude Code хранится так; симлинк ~/.local/bin/claude
#      может протухнуть после обновлений — fallback'имся к самой свежей).
_resolve_claude() {
    [[ -n "${CLAUDE_BIN:-}" ]] && { echo "$CLAUDE_BIN"; return; }
    local p
    p="$(command -v claude 2>/dev/null || true)"
    [[ -n "$p" && -x "$p" ]] && { echo "$p"; return; }
    local mac_root="$HOME/Library/Application Support/Claude/claude-code"
    if [[ -d "$mac_root" ]]; then
        # самая свежая версия по mtime
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

# 1) Получить полную карточку задачи (title + description + acceptance + links)
TASK_JSON="$(curl -fsS "${KANBAN_URL}/api/tasks/${TASK_ID}")"

TITLE="$(echo "$TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("title",""))')"
DESC="$(echo "$TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("description",""))')"
ACCEPTANCE="$(echo "$TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("acceptance",""))')"

# 2) Найти директорию проекта (project.path)
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
    # Если project_id не передан — используем текущую директорию.
    PROJ_PATH="$PWD"
fi

if [[ ! -d "$PROJ_PATH" ]]; then
    echo "ERROR: project path does not exist: $PROJ_PATH" >&2
    exit 1
fi

# 3) Сформировать prompt для агента
PROMPT=$(cat <<EOF
Ты подключён к agent-kanban через MCP-сервер. Тебе нужно взять и
полностью реализовать задачу ${TASK_ID}.

Workflow:
  1. kanban_pull(task_id="${TASK_ID}")  — забрать задачу (approved → analyst).
  2. Прочитать описание ниже, разобраться, написать план как комментарий
     через kanban_comment.
  3. kanban_move(task_id="${TASK_ID}", to_status="in_progress")
  4. Реализовать. По ходу — kanban_comment с прогрессом, kanban_link на PR/файлы.
  5. kanban_move(task_id="${TASK_ID}", to_status="testing", comment="готово к проверке")
  6. Остановиться. Не двигай в "uat" / "done" — это решает человек.

=== Контекст задачи ===
ID:          ${TASK_ID}
Title:       ${TITLE}

Description:
${DESC}

Acceptance criteria:
${ACCEPTANCE}
=== Конец контекста ===

Если задача неоднозначная — добавь kanban_comment с уточняющими вопросами
и переведи задачу в "blocked". Не пытайся угадывать.
EOF
)

# 4) Запуск Claude Code в headless-режиме в директории проекта.
# Флаг `-p`/`--print` означает «выполнить и выйти» (без интерактивного REPL).
# `--permission-mode=acceptEdits` — для автоматизации (без ручных confirms).
# Перенаправляем stdout/stderr в лог чтобы видеть прогресс.

LOG_DIR="${HOME}/Library/Logs/agent-kanban"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/launcher-${TASK_ID}-$(date +%Y%m%d-%H%M%S).log"

echo "[$(date -u +%FT%TZ)] launch ${TASK_ID} in ${PROJ_PATH} (log: ${LOG_FILE})" >&2

# Имя MCP-сервера в .mcp.json пользователя (alias). Дефолт — "agent-kanban",
# но у тебя может быть "finops-kanban" / "kanban" / любое.
MCP_ALIAS="${KANBAN_MCP_ALIAS:-agent-kanban}"

# Разрешённые tools в headless режиме. Без --allowedTools Claude видит MCP,
# но отказывается их вызывать с "tool not allowed".
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

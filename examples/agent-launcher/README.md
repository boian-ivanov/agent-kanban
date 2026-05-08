# Agent launcher

Скрипты, которые канбан вызывает через `run_command`-action при движении
карточки в определённый статус. Цель — **«перетащил задачу в Согласовано
→ агент сам её разобрал и довёл до Тестирования»**.

## Что лежит

| Файл | Что делает |
|---|---|
| [`launch-claude.sh`](launch-claude.sh) | Запускает Claude Code (`claude -p`) в headless-режиме, в директории Claude Code-проекта. Передаёт в prompt task description + acceptance + workflow-инструкцию. Агент сам через MCP проводит задачу analyst → in_progress → testing. |

## Как подключить

1. **Включи Claude Code-проект на канбан-проект**:
   - В UI канбана → `⋯` рядом с проектом → укажи `Директория проекта`
     (например, `~/code/myapp`).
   - В этой директории должен быть `.mcp.json` с подключением к
     agent-kanban (см. [docs/INTEGRATION.md](../../docs/INTEGRATION.md#1-claude-code-mcp)).
   - Установи в `.mcp.json` `env: { "KANBAN_PROJECT_ID": "myproj" }` —
     чтоб `kanban_create` без аргумента шёл в твой проект.

2. **Добавь rule в `kanban_data/rules.json`**:
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

   Hot-reload по mtime — рестарт не нужен.

3. **Тест**: создай задачу в backlog → перетащи в `Согласовано` → через
   секунду агент должен взять её (видишь `kanban_pull` в history,
   статус сменился на `analyst`).

## Поддержанные placeholders

В `args` подставляются из контекста события `task_moved`:

| Placeholder | Содержит |
|---|---|
| `{task_id}` | ID задачи (T-XXX) |
| `{title}` | заголовок задачи |
| `{project_id}` | slug проекта |
| `{from_status}` | из какой колонки переехала |
| `{to_status}` | в какую колонку переехала |

## Безопасность

Скрипт выполняется на сервере канбана (= localhost). Никакого sandboxing'а
нет — `cmd` и `args` запускаются как есть. Не пиши в `rules.json` команды,
которые ты бы не запустил у себя в shell.

## Свой launcher

Можно подменить `launch-claude.sh` на любой другой:
- `launch-opencode.sh` — для opencode (`opencode --task ...`)
- `launch-aider.sh` — для Aider
- `launch-prompt.sh` — твоя собственная обёртка с другим LLM

Главное — скрипт должен:
1. Не блокировать вызывающего. Запускай агента в фоне (`nohup ... &`).
2. Выйти быстро (≤ 1 сек). Канбан не ждёт твой агент — это длинная задача.
3. Логировать куда-нибудь (опц.) — agent работает сам, у него нет UI.

## Concurrency

Если 3 задачи перешли в Согласовано подряд — канбан запустит 3 параллельных
launcher'а. Если у тебя есть лимит на параллельные сессии — реализуй
семафор/lockfile в самом скрипте:

```bash
# В начале launch-claude.sh:
LOCK="/tmp/agent-kanban-launcher.lock"
exec 200>"$LOCK"
flock -n 200 || { echo "another launcher running, queueing ${TASK_ID}" >&2; exit 0; }
```

Или поставь свою очередь сверху.

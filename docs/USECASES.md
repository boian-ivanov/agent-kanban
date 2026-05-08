# Use Cases — agent-kanban

Как канбан реально используется. Каждый UC — короткий сценарий: кто
действующий, что делает, что получает. Здесь только то, что система
поддерживает прямо сейчас (без "будущих фич").

## Жизненный цикл задачи

```
            пользователь                                агент (Claude/Cline/...)
                ↓                                              ↓
   ┌────────┐  push  ┌──────────┐  pull  ┌──────────┐  ────→  ┌────────────┐
   │ Backlog│ ─────→ │Согласовано│ ───── │ Аналитика│         │  В работе  │
   └────────┘        └──────────┘        └──────────┘         └────────────┘
                                                                    │
                                                                    ↓
   ┌────────┐  ←──── ┌──────────┐  ←─── ┌────────────┐
   │ Закрыто│  принять│ Приёмка  │  done│Тестирование│
   └────────┘        └──────────┘       └────────────┘

   Заблокировано — параллельная колонка для всего что застряло.
   Отменено — terminal state «не делаем».
```

«Владельцы» колонок (см. `kanban_columns()`) — это **семантическая
подсказка**, не контроль доступа. UI и API позволяют двигать карту куда
угодно. Колонки можно редактировать в `kanban_store/store.py`
(`STATUSES` + `status_meta()`).

---

## UC-0: First-time setup

**Кто:** новый пользователь, только клонировал репо.

**Шаги:**
1. `git clone … && cd agent-kanban`
2. `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. `.venv/bin/python -m kanban_ui` → http://localhost:7777
4. Видит **проект `default`** с пустой доской, 9 колонок, sidebar слева, кнопка `+ Задача` в topbar.

**Результат:** работает локально, БД в `tasks.db`, никаких аккаунтов и облака.

Опционально: `bash scripts/install_launchd.sh install` — авто-старт при логине.

---

## UC-1: Соло разработчик с Claude Code

**Кто:** один человек, Claude Code открыт в каком-то проекте.

**Trigger:** хочу, чтобы агент видел мои задачи и сам мог двигать карты по мере работы.

**Шаги:**
1. Создать проект в канбане (`+ Новый проект` → имя + slug + директория = текущий repo).
2. В корень репо положить `.mcp.json`:
   ```jsonc
   { "mcpServers": { "agent-kanban": {
       "type": "stdio",
       "command": "/abs/.venv/bin/python",
       "args": ["-m", "kanban_mcp"],
       "cwd":  "/abs/agent-kanban",
       "env":  { "KANBAN_PROJECT_ID": "myproj" }
   }}}
   ```
3. Перезапустить Claude Code (`/mcp restart agent-kanban`).
4. В чате: «*покажи что у меня в работе*» → агент вызывает `kanban_my_active(assignee="claude", project_id="myproj")`.

**Результат:** агент знает контекст, может `kanban_pull` задачу из Согласовано, по ходу работы добавлять `kanban_comment`, в конце `kanban_move(task_id, "testing")`.

---

## UC-2: Командный канал в Slack/Telegram

**Кто:** команда из 2-5 человек, хочет видеть в чате когда что-то двигается.

**Шаги:**
1. Получить incoming-webhook URL Slack или `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT>` для Telegram.
2. Создать `kanban_data/webhooks.json`:
   ```json
   { "webhooks": [
     { "name": "Slack #dev", "url": "https://hooks.slack.com/...",
       "events": ["task_moved", "task_created"], "format": "slack" }
   ]}
   ```
3. Никаких рестартов — hot-reload по mtime.

**Результат:** при `task_moved` в чат летит `→ [Project] T-123 «Title»: backlog → in_progress — comment`. Доставка fire-and-forget, основной HTTP не блокируется. Логи доставок в `/api/automation/status.webhooks`.

---

## UC-3: Импорт существующего проекта (legacy `PROJECT-PLAN.md`)

**Кто:** пользователь, у которого уже есть markdown-планы в репо.

**Trigger:** хочу подключить эти файлы к доске, не переписывая их вручную.

**Шаги:**
1. `+ Новый проект` → ID slug + директория → клик «Выбрать…» → нативный Finder.
2. В табе **«Существующие планы»** канбан скан Files в директории, показывает кандидатов с приоритетами (`PROJECT-PLAN.md`, `BACKLOG.md`, `TODO.md` сверху). Plan-файлы (prio ≤ 5) автоматически отмечены ☑.
3. Клик «Сохранить».

**Что происходит:**
- Парсер читает каждый файл, разбирает `## ...` секции:
  - Канонические заголовки (`## Backlog`, `## Done`, `## Бэклог`) → задачи в соответствующую колонку
  - Любые другие (`## 🔴 Tier 0`, `## v2 этапы`) → в `backlog` с именем секции в `description` как контекст
- Создаёт карточки идемпотентно (по uniqueness `project_id + title`)
- Дописывает блок-инструкцию в `CLAUDE.md` — Claude в этой папке знает, что новые задачи писать в plan-файл

**Результат:** проект Aizav2 с 183 задачами через 1 секунду без копипасты.

---

## UC-4: Daily standup за 30 секунд

**Кто:** пользователь утром, открыл Claude Code.

**Шаги:**
1. *«Что у меня сейчас в работе?»* → `kanban_my_active(assignee="claude")`
2. *«А что застряло?»* → `kanban_list(status="blocked")`
3. *«Какие приоритеты в backlog'е?»* → `kanban_list(status="backlog")` + клиент фильтрует priority=high

**Результат:** план дня без открытия браузера.

Альтернатива в UI: `/p/myproj` → фильтр HIGH → density compact → все плотно видно.

---

## UC-5: Полная сессия агента над одной задачей

**Кто:** Claude Code сессия, я говорю «возьми задачу T-008 и сделай её».

**Flow:**
1. `kanban_get("T-008")` — читает description + acceptance + history.
2. `kanban_pull("T-008")` — атомарно `approved → analyst, assignee=claude`.
3. *(анализ кода, план)* → `kanban_comment("T-008", "План: ...")`.
4. `kanban_move("T-008", "in_progress", comment="начал писать парсер")`.
5. *(работа)* → `kanban_link("T-008", "pr", "https://github.com/.../pull/42")`.
6. `kanban_move("T-008", "testing", comment="готов код+тесты")` — webhook летит в Slack.
7. Я в UI вижу карточку в Тестировании, проверяю → drag-drop в **Приёмка** → **Закрыто**.

**Результат:** задача прошла полный workflow, в history лежат все шаги с актором (`claude`), линки на PR, комментарии.

---

## UC-6: Авто-архив старых задач

**Кто:** пользователь, доска засоряется done-картами.

**Шаги:** `kanban_data/rules.json`:
```json
{ "rules": [{
  "name": "Закрытые > 30 дней → отменено",
  "trigger": { "type": "task_idle", "status": "done", "days": 30 },
  "action":  { "type": "move_to", "status": "cancelled",
               "comment": "Авто-архив" }
}]}
```

**Результат:** каждые 60 секунд (`KANBAN_AUTOMATION_INTERVAL`) engine проверяет правила и двигает старые карты. Все действия с actor=`automation` в history.

Поддержанные триггеры: `task_idle` (по `moved_at`), `task_count_in_status` (gt/lt).
Поддержанные action'ы: `move_to`, `add_comment`, `set_priority`.

---

## UC-7: Inbox capture (ad-hoc заметки)

**Кто:** работаю и в голову пришла идея.

**Шаги:**
- Создаёшь `~/Projects/agent-kanban/kanban_data/inbox/quick-note.md`:
  ```markdown
  ---
  title: Кнопка «Pin» для важных задач
  priority: low
  size: S
  ---
  Закрепляет карточку наверху колонки.
  ```
- Через 5 секунд карточка появилась в backlog. Файл переехал в `inbox/processed/2026-05-09/quick-note.md`.

Можно направить watcher на любую папку через `KANBAN_INBOX_DIR`. Например, `~/.claude/projects/<encoded>/memory/inbox/` — Claude свои session-резюме капает туда, канбан их видит.

---

## UC-8: Несколько Claude Code-проектов одновременно

**Кто:** в течение дня переключаюсь между 3 проектами в разных директориях.

**Шаги:**
1. В каждом репо свой `.mcp.json` с `KANBAN_PROJECT_ID="<slug-of-this-project>"`.
2. Когда я открыт в `~/code/myapp` — Claude видит и двигает только задачи `myapp`.
3. В UI: `Cmd+Shift+R` → URL `/p/myapp` или клик в sidebar.

**Результат:** контекст не путается, history каждого проекта изолирована.

В UI sidebar помнит последний открытый проект (`localStorage.kb.lastProject`) — куда заходил последний раз, туда и попадаешь.

---

## UC-9: Чисто браузерная работа (без агента)

**Кто:** не использую AI вовсе или использую веб-чат отдельно.

**Шаги:**
- `+ Задача` или `n` — создать.
- Drag-drop карточек между колонками.
- Колесико мыши на пустом месте → горизонтальный pan по доске.
- Hover на колонку → `+` для quick-add (одна строка title, Enter — создать).
- Клик по карточке → модалка с историей, links, blockers, комментариями.
- Поиск (`/`), фильтр-чипы (HIGH / Блокер / Агенты / Свободные).
- Density toggle (`d`) для плотного режима на 100+ задачах.

**Результат:** канбан ничем не отличается от Trello/Jira, просто локально и без аккаунтов.

---

## UC-11: Auto-launch агента при перемещении в Согласовано

**Кто:** соло dev, перетаскивает задачу из Бэклога в Согласовано и хочет, чтобы агент сам её разобрал.

**Trigger:** drag-drop в UI или `POST /api/tasks/{id}/move` → `to_status="approved"`.

**Шаги настройки:**

1. **Привязать канбан-проект к директории Claude Code-проекта** (UI → `⋯` → «Директория проекта»). В этой директории должен быть `.mcp.json` с подключением к agent-kanban (см. [INTEGRATION.md](INTEGRATION.md#1-claude-code-mcp)).

2. **Подключить готовый launcher** — `examples/agent-launcher/launch-claude.sh`. Сделать его executable (`chmod +x`).

3. **Добавить rule в `kanban_data/rules.json`:**
   ```json
   {
     "rules": [{
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
     }]
   }
   ```

   Hot-reload по mtime — рестарт не нужен.

**Что происходит при drag-drop в Согласовано:**

1. UI/API → `move_task(T-027, to_status="approved")` → запись в БД.
2. Endpoint emits `task_moved` event → rule engine matched, запускает в фоне `launch-claude.sh T-027 myproj`.
3. Скрипт получает task description через REST, формирует prompt, запускает `claude -p "..." --permission-mode=acceptEdits` в директории проекта в фоне (`nohup ... &`).
4. Claude через MCP делает `kanban_pull(T-027)` (approved → analyst, assignee=claude), пишет план через `kanban_comment`, переходит в `in_progress`, реализует, в конце `kanban_move(T-027, "testing", comment="готов к проверке")`.
5. Я в UI вижу карточку в Тестировании, проверяю → drag в **Приёмка** → **Закрыто**.

**Триггеры (rule.trigger.type=task_moved):**
- `to_status` (обязательно) — куда переехало.
- `from_status` (опц.) — откуда. Если задано — фильтрует.
- `project_id` (опц.) — ограничить одним проектом.

**Action `run_command`:**
- `cmd` — путь к executable (на сервере).
- `args` — список с placeholder'ами: `{task_id}`, `{title}`, `{project_id}`, `{from_status}`, `{to_status}`.
- `log_file` (опц.) — если указан, stdout/stderr скрипта пишется туда.

Скрипт запускается в фоне (`asyncio.create_subprocess_exec`), канбан **не ждёт** его завершения — agent сессия может работать минуты-часы.

**Важно про безопасность:** `cmd` и `args` выполняются на сервере без sandboxing'а. Скрипт должен быть твой, не дёргать webhook'и от ненадёжных источников.

**Свой агент вместо Claude Code:** скопируй `launch-claude.sh` → `launch-myagent.sh`, замени `claude -p` на свой CLI (`opencode`, `aider`, OpenAI SDK обёртка). Контракт прежний: получи task через REST, запусти агента в фоне.

---

## UC-10: Snapshot для отчёта

**Кто:** конец недели, нужно показать что сделано.

**Шаги:**
- В topbar `Snapshot` или `POST /api/snapshot`.
- В `snapshots/2026-05-09.json` лежит полный JSON-дамп: все проекты + все задачи + history.

**Результат:** артефакт для коммита/отправки/анализа.

Snapshots игнорятся git'ом (см. `.gitignore`); если нужно версионить — измени правило.

---

## Что **не входит** в текущий scope

- Multi-user auth — канбан слушает только `127.0.0.1`. Хочешь команду — поднимай nginx с basic-auth или Authelia сверху, либо ждёшь Roadmap «multi-user mode».
- Bidirectional `PLAN.md` ↔ kanban — пока односторонний (file → канбан). Изменения в UI **не пишутся обратно** в файл (см. Roadmap).
- Sub-tasks / иерархия — `task_blockers` есть как зависимости (DAG), но настоящей вложенности нет.
- Time tracking — есть `moved_at` и `created_at` в history, агрегации поверх — DIY через `/api/snapshot`.
- GitHub Issues sync — поле `git` в project_source хранит URL+token, но импорт issues пока не реализован.

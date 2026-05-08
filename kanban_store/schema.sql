-- Kanban schema (SQLite)
-- v2: добавлены projects + tasks.project_id (миграция в Store._migrate_v2).

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,                    -- 'finops', 'kanban-dev', ...
    name        TEXT NOT NULL,                       -- 'FinOps', 'Kanban Dev'
    color       TEXT NOT NULL DEFAULT '#F10D30',     -- акцентный цвет проекта (hex)
    icon        TEXT NOT NULL DEFAULT '',            -- 1-2 символа: 'F', 'KB', 'AI'
    sort_order  INTEGER NOT NULL DEFAULT 0,          -- порядок в свитчере
    archived    INTEGER NOT NULL DEFAULT 0,          -- 0/1
    path        TEXT,                                -- директория Claude Code-проекта (опц.)
    created_at  TEXT NOT NULL                        -- ISO8601
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,                -- T-001, T-002, ...
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'backlog', -- backlog/approved/analyst/in_progress/testing/uat/done/blocked/cancelled
    priority        TEXT NOT NULL DEFAULT 'normal',  -- high/normal/low
    size            TEXT NOT NULL DEFAULT 'M',       -- S/M/L
    assignee        TEXT,                            -- user / agent:<name> / NULL
    description     TEXT NOT NULL DEFAULT '',
    acceptance      TEXT NOT NULL DEFAULT '',
    external_blocker TEXT,                           -- "DevOps: roles monitoring.viewer"
    created_at      TEXT NOT NULL,                   -- ISO8601
    moved_at        TEXT NOT NULL,                   -- ISO8601, last status change
    column_order    INTEGER NOT NULL DEFAULT 0,      -- порядок внутри колонки (для drag-drop)
    project_id      TEXT NOT NULL DEFAULT 'default'  -- FK → projects.id
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, column_order);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
-- idx_tasks_project_status создаётся в Store._migrate_v2 (после ALTER TABLE для старых БД).

CREATE TABLE IF NOT EXISTS task_links (
    task_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type     TEXT NOT NULL,                          -- memory/file/pr/url
    value    TEXT NOT NULL,
    PRIMARY KEY (task_id, type, value)
);

CREATE TABLE IF NOT EXISTS task_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,                      -- user/agent:<name>
    action       TEXT NOT NULL,                      -- create/move/comment/assign
    from_status  TEXT,
    to_status    TEXT,
    comment      TEXT
);

CREATE INDEX IF NOT EXISTS idx_history_task ON task_history(task_id, ts);

CREATE TABLE IF NOT EXISTS task_blockers (
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    blocker_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, blocker_id),
    CHECK (task_id != blocker_id)
);

CREATE TABLE IF NOT EXISTS project_sources (
    project_id    TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    type          TEXT NOT NULL,              -- 'plan_md' | 'git'
    config        TEXT NOT NULL,              -- JSON: {file, repo_url, ...}
    last_sync_at  TEXT,                       -- ISO8601
    created_at    TEXT NOT NULL
);

-- meta для миграций
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '4');
INSERT OR IGNORE INTO meta(key, value) VALUES ('next_id', '1');

-- Дефолтный проект — берётся из env ``KANBAN_DEFAULT_PROJECT_ID`` / ``..._NAME``
-- (см. Store._seed_default_project). Если не задано, создаётся 'default'/'Default'.
-- Существующие задачи получат этот project_id через _migrate_v2().

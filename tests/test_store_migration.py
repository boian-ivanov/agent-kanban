"""Store schema migration tests: v5 → v6 (Epic->Story->Ticket hierarchy,
task_chat + task_runs tables).

Frozen snapshot of the v5 schema (pre-parent_id/pre-kind) so the test
does not depend on git history.
"""

from __future__ import annotations

import sqlite3

from kanban_store.store import Store

V5_SCHEMA = """
CREATE TABLE projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    color       TEXT NOT NULL DEFAULT '#F10D30',
    icon        TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    path        TEXT,
    model       TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'backlog',
    priority        TEXT NOT NULL DEFAULT 'normal',
    size            TEXT NOT NULL DEFAULT 'M',
    assignee        TEXT,
    description     TEXT NOT NULL DEFAULT '',
    acceptance      TEXT NOT NULL DEFAULT '',
    external_blocker TEXT,
    created_at      TEXT NOT NULL,
    moved_at        TEXT NOT NULL,
    column_order    INTEGER NOT NULL DEFAULT 0,
    project_id      TEXT NOT NULL DEFAULT 'default'
);
CREATE TABLE task_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    from_status  TEXT,
    to_status    TEXT,
    comment      TEXT
);
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO meta(key, value) VALUES ('schema_version', '5');
INSERT INTO meta(key, value) VALUES ('next_id', '10');
"""


def _make_v5_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(V5_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, moved_at) "
        "VALUES ('T-001', 'old task', 'done', '2026-01-01', '2026-01-02')"
    )
    conn.execute(
        "INSERT INTO task_history (task_id, ts, actor, action, to_status, comment) "
        "VALUES ('T-001', '2026-01-02', 'user', 'move', 'done', 'migrate me')"
    )
    conn.commit()
    conn.close()


def test_migrate_v5_to_v6_lossless(tmp_path):
    db = tmp_path / "test.db"
    _make_v5_db(db)

    Store(db)  # runs _migrate (v5 -> v6)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        # version bumped
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()["value"]
            == "7"
        )

        # tasks columns added
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert {"parent_id", "kind"} <= cols

        # backfill: existing row is kind='task', parent_id NULL
        row = conn.execute(
            "SELECT kind, parent_id FROM tasks WHERE id='T-001'"
        ).fetchone()
        assert row["kind"] == "task"
        assert row["parent_id"] is None

        # data preserved (ids/status/history)
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        hist = conn.execute(
            "SELECT task_id, to_status, comment FROM task_history"
        ).fetchone()
        assert (hist["task_id"], hist["to_status"], hist["comment"]) == (
            "T-001",
            "done",
            "migrate me",
        )

        # new tables + parent index exist
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"task_chat", "task_runs"} <= tables
        idx = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_tasks_parent" in idx
    finally:
        conn.close()


def test_migrate_v6_idempotent(tmp_path):
    db = tmp_path / "test.db"
    _make_v5_db(db)

    Store(db)
    Store(db)  # second open must not error or re-migrate

    conn = sqlite3.connect(str(db))
    try:
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            == "7"
        )
    finally:
        conn.close()


def test_fresh_db_is_v7(tmp_path):
    db = tmp_path / "fresh.db"
    Store(db)
    conn = sqlite3.connect(str(db))
    try:
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            == "7"
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert {"parent_id", "kind"} <= cols
    finally:
        conn.close()

"""Per-project ticket numbering (projects.code + project_seq).

Covers the v6 → v7 migration (projects.code column + project_seq table)
and Store._next_id per-project behavior: ``{code}-{seq:03d}`` ids restart
at 001 per project, legacy ``T-###`` ids stay untouched.
"""

from __future__ import annotations

import sqlite3

import pytest

from kanban_store.store import Store

V6_SCHEMA = """
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
    project_id      TEXT NOT NULL DEFAULT 'default',
    parent_id       TEXT REFERENCES tasks(id),
    kind            TEXT NOT NULL DEFAULT 'task'
        CHECK (kind IN ('epic','story','task'))
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
INSERT INTO meta(key, value) VALUES ('schema_version', '6');
INSERT INTO meta(key, value) VALUES ('next_id', '317');
"""


def _make_v6_db(path, *, with_projects: bool = False) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(V6_SCHEMA)
    if with_projects:
        conn.execute(
            "INSERT INTO projects (id, name, created_at) VALUES "
            "('default', 'Default', '2026-01-01'), "
            "('salon-platform', 'Salon Platform', '2026-01-01')"
        )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, moved_at) "
        "VALUES ('T-001', 'legacy task', 'done', '2026-01-01', '2026-01-02')"
    )
    conn.commit()
    conn.close()


def test_migrate_v6_to_v7(tmp_path):
    db = tmp_path / "test.db"
    _make_v6_db(db, with_projects=True)

    Store(db)  # runs _migrate (v6 -> v7)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()["value"]
            == "8"
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
        assert "code" in cols
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "project_seq" in tables
        # legacy data preserved
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        conn.close()


def test_migrate_v7_idempotent(tmp_path):
    db = tmp_path / "test.db"
    _make_v6_db(db, with_projects=True)

    Store(db)
    Store(db)  # second open must not error or re-migrate

    conn = sqlite3.connect(str(db))
    try:
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            == "8"
        )
    finally:
        conn.close()


def test_per_project_ids_restart_at_001(tmp_path):
    store = Store(tmp_path / "test.db")
    store.create_project("salon-platform", "Salon Platform", code="SP")
    store.create_project("agent-kanban", "Agent Kanban", code="AK")

    sp1 = store.create_task("SP task 1", project_id="salon-platform")
    sp2 = store.create_task("SP task 2", project_id="salon-platform")
    ak1 = store.create_task("AK task 1", project_id="agent-kanban")

    assert sp1.id == "SP-001"
    assert sp2.id == "SP-002"
    assert ak1.id == "AK-001"
    # both projects have their own -001
    assert sp1.id.endswith("-001") and ak1.id.endswith("-001")
    assert sp1.id != ak1.id


def test_legacy_project_falls_back_to_global_ids(tmp_path):
    db = tmp_path / "test.db"
    _make_v6_db(db, with_projects=True)

    store = Store(db)  # 'default' project has no code
    t = store.create_task("legacy new task", project_id="default")
    assert t.id == "T-317"  # continues meta('next_id')=317
    t2 = store.create_task("legacy new task 2", project_id="default")
    assert t2.id == "T-318"


def test_legacy_tasks_still_readable_and_movable(tmp_path):
    db = tmp_path / "test.db"
    _make_v6_db(db, with_projects=True)

    store = Store(db)
    t = store.get_task("T-001")
    assert t is not None and t.title == "legacy task"

    moved = store.move_task("T-001", "cancelled", actor="user")
    assert moved.status == "cancelled"
    assert store.get_task("T-001").status == "cancelled"


def test_explicit_task_id_still_honored(tmp_path):
    store = Store(tmp_path / "test.db")
    store.create_project("salon-platform", "Salon Platform", code="SP")
    t = store.create_task(
        "manual id", task_id="SP-042", project_id="salon-platform"
    )
    assert t.id == "SP-042"
    # sequence not consumed by the manual id
    assert store.create_task("next", project_id="salon-platform").id == "SP-001"


def test_code_change_before_first_ticket(tmp_path):
    store = Store(tmp_path / "test.db")
    store.create_project("salon-platform", "Salon Platform", code="SP")

    # change allowed with only legacy T-### tasks
    store.create_task("legacy", project_id="default")
    store.update_project("salon-platform", code="SLN")
    assert store.get_project("salon-platform").code == "SLN"

    # blocked once a {code}-### ticket exists
    store.create_task("first real ticket", project_id="salon-platform")
    with pytest.raises(ValueError, match="tickets already issued"):
        store.update_project("salon-platform", code="SAL")


def test_duplicate_code_rejected(tmp_path):
    store = Store(tmp_path / "test.db")
    store.create_project("salon-platform", "Salon Platform", code="SP")
    with pytest.raises(ValueError, match="already used"):
        store.create_project("salon-platform-2", "Salon 2", code="SP")


def test_code_change_clears_back_to_legacy(tmp_path):
    store = Store(tmp_path / "test.db")
    store.create_project("salon-platform", "Salon Platform", code="SP")
    store.update_project("salon-platform", code="")
    assert store.get_project("salon-platform").code is None
    t = store.create_task("legacy again", project_id="salon-platform")
    assert t.id.startswith("T-")

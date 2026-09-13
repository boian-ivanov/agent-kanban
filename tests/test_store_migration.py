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
            == "9"
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
            == "9"
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
            == "9"
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert {"parent_id", "kind"} <= cols
    finally:
        conn.close()


def test_migrate_v5_to_v8_adds_constraints(tmp_path, monkeypatch):
    """Full migration chain (v5 -> v8): the constraints column appears and
    legacy data survives — guards against dropping an intermediate _migrate
    call (v5/v6/v7) from the chain."""
    # test_plan_md_loose.py leaks KANBAN_DEFAULT_PROJECT_ID — pin it so the
    # v2 migration seeds the canonical 'default' project row.
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    db = tmp_path / "test.db"
    _make_v5_db(db)

    Store(db)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()["value"]
            == "9"
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
        assert {"code", "constraints", "model", "path"} <= cols
        # legacy task preserved through every migration
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        # the default project (seeded during v2 migration) has no constraints
        assert conn.execute(
            "SELECT constraints FROM projects WHERE id='default'"
        ).fetchone()[0] is None
    finally:
        conn.close()


def test_project_constraints_roundtrip(tmp_path):
    """projects.constraints: set via create, overridable via update, [] clears."""
    store = Store(tmp_path / "t.db")
    p = store.create_project(
        "agent-kanban",
        "Agent Kanban",
        path="/tmp/ak",
        constraints=["no commit", "pytest gate"],
    )
    assert p.constraints == ["no commit", "pytest gate"]
    assert p.to_public()["constraints"] == ["no commit", "pytest gate"]

    p2 = store.update_project("agent-kanban", constraints=["only pytest"])
    assert p2.constraints == ["only pytest"]

    p3 = store.update_project("agent-kanban", constraints=[])
    assert p3.constraints == []

    # None leaves the value alone
    p4 = store.update_project("agent-kanban", name="Renamed")
    assert p4.constraints == []

    # unset (never configured) is None, not [] — the driver must tell
    # "no board opinion" (seed fallback) apart from "cleared" (generic gate)
    plain = store.create_project("plain", "Plain")
    assert plain.constraints is None
    assert plain.to_public()["constraints"] is None


def test_migrate_v8_to_v9_adds_worktrees(tmp_path):
    """v9: projects.worktrees + task_runs.worktree, additive and idempotent."""
    db = tmp_path / "v8.db"
    # A v8 database: projects + task_runs without the lane columns.
    conn = sqlite3.connect(str(db))
    conn.executescript(V5_SCHEMA)
    conn.executescript(
        """
        CREATE TABLE task_runs (
            task_id      TEXT PRIMARY KEY,
            pid          INTEGER,
            started_at   TEXT,
            ended_at     TEXT,
            model        TEXT,
            role         TEXT,
            status       TEXT,
            tokens_used  INTEGER,
            control_port INTEGER
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '8');
        """
    )
    conn.execute(
        "INSERT INTO projects (id, name, created_at) VALUES ('p', 'P', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, moved_at) "
        "VALUES ('T-001', 'legacy task', 'done', '2026-01-01', '2026-01-02')"
    )
    conn.commit()
    conn.close()

    store = Store(db)  # runs the migrations
    store.register_run("T-001", worktree="/tmp/lane/1", status="running")
    run = store.get_run("T-001")
    assert run["worktree"] == "/tmp/lane/1"

    # projects.worktrees is NULL on a migrated project (lane mode off) and
    # round-trips once set — the driver reads it to decide shared vs lane.
    assert store.get_project("p").worktrees is None
    updated = store.update_project(
        "p", worktrees={"enabled": True, "root": "/tmp/lanes", "count": 2}
    )
    assert updated.worktrees == {"enabled": True, "root": "/tmp/lanes", "count": 2}
    assert updated.to_public()["worktrees"]["count"] == 2

    # idempotent: a second open neither errors nor resets the config
    again = Store(db)
    assert again.get_project("p").worktrees["enabled"] is True

    conn = sqlite3.connect(str(db))
    try:
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            == "9"
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
        assert "worktrees" in cols
        run_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
        assert "worktree" in run_cols
    finally:
        conn.close()


def test_worktrees_corrupt_json_disables_lane_mode(tmp_path):
    """Corrupt config must fail safe (shared tree), never enable lanes."""
    db = tmp_path / "t.db"
    store = Store(db)
    store.create_project("p", "P")
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE projects SET worktrees='{not json' WHERE id='p'")
    conn.commit()
    conn.close()
    assert Store(db).get_project("p").worktrees is None


def test_project_worktrees_roundtrip(tmp_path):
    """worktrees: set via create, overridable via update, None leaves it alone."""
    store = Store(tmp_path / "t.db")
    p = store.create_project(
        "salon-platform",
        "Salon Platform",
        worktrees={"enabled": True, "root": "/tmp/sp-lanes", "count": 2},
    )
    assert p.worktrees == {"enabled": True, "root": "/tmp/sp-lanes", "count": 2}

    p2 = store.update_project("salon-platform", worktrees={"enabled": False})
    assert p2.worktrees == {"enabled": False}

    p3 = store.update_project("salon-platform", name="Renamed")
    assert p3.worktrees == {"enabled": False}

    plain = store.create_project("plain", "Plain")
    assert plain.worktrees is None

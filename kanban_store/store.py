"""Kanban — SQLite store.

Single source of truth: ``tasks.db`` (gitignored), or the path from env
``KANBAN_DB`` (see Store.__init__).

Public API:
    Store.list_tasks(status=..., assignee=...)
    Store.get_task(task_id)
    Store.create_task(title, status="backlog", ...)
    Store.move_task(task_id, to_status, actor, comment=None)
    Store.assign_task(task_id, assignee, actor)
    Store.add_comment(task_id, text, actor)
    Store.add_link(task_id, type, value)
    Store.set_blockers(task_id, blocker_ids)
    Store.snapshot()  -> dict (for JSON snapshots)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

DEFAULT_PROJECT_ID = os.environ.get("KANBAN_DEFAULT_PROJECT_ID", "default")
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ============================================================================
# Status model
# ============================================================================

# 9 columns in left-to-right UI order.
STATUSES: list[str] = [
    "backlog",
    "approved",
    "analyst",
    "in_progress",
    "testing",
    "uat",
    "done",
    "blocked",
    "cancelled",
]


def status_meta() -> list[dict[str, str]]:
    """Column metadata for the UI (label + cssClass)."""
    return [
        {"id": "backlog",     "title": "Backlog",      "owner": "user"},
        {"id": "approved",    "title": "Approved",     "owner": "agent"},
        {"id": "analyst",     "title": "Analyst",      "owner": "agent"},
        {"id": "in_progress", "title": "In progress",  "owner": "agent"},
        {"id": "testing",     "title": "Testing",      "owner": "agent"},
        {"id": "uat",         "title": "UAT",          "owner": "user"},
        {"id": "done",        "title": "Done",         "owner": "user"},
        {"id": "blocked",     "title": "Blocked",      "owner": "any"},
        {"id": "cancelled",   "title": "Cancelled",    "owner": "user"},
    ]


# ============================================================================
# Models
# ============================================================================


@dataclass
class TaskHistory:
    id: int
    task_id: str
    ts: str
    actor: str
    action: str
    from_status: str | None
    to_status: str | None
    comment: str | None


@dataclass
class Task:
    id: str
    title: str
    status: str
    priority: str
    size: str
    assignee: str | None
    description: str
    acceptance: str
    external_blocker: str | None
    created_at: str
    moved_at: str
    column_order: int
    project_id: str = DEFAULT_PROJECT_ID
    links: list[dict[str, str]] = field(default_factory=list)
    history: list[TaskHistory] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def to_public(self) -> dict[str, Any]:
        d = asdict(self)
        d["history"] = [asdict(h) for h in self.history]
        return d


@dataclass
class Project:
    id: str
    name: str
    color: str
    icon: str
    sort_order: int
    archived: bool
    created_at: str
    path: str | None = None
    task_counts: dict[str, int] = field(default_factory=dict)
    total_tasks: int = 0

    def to_public(self) -> dict[str, Any]:
        return asdict(self)


# ============================================================================
# Store
# ============================================================================


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Thread-safe wrapper around SQLite. One instance per process."""

    _lock = threading.RLock()

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = Path(__file__).resolve().parent.parent / "tasks.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; explicit transactions use BEGIN
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
        with self._lock:
            self._conn.executescript(schema)
            self._migrate_v2()
            self._migrate_v3()
            self._migrate_v4()

    def _migrate_v4(self) -> None:
        """v3 → v4: project_sources table (created via schema.sql,
        this method only bumps the version)."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row and int(row["value"]) < 4:
            self._conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")

    def _migrate_v3(self) -> None:
        """v2 → v3: projects.path TEXT (Claude Code project directory)."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(projects)").fetchall()}
        if "path" not in cols:
            self._conn.execute("ALTER TABLE projects ADD COLUMN path TEXT")
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row and int(row["value"]) < 3:
            self._conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")

    def _migrate_v2(self) -> None:
        """v1 → v2: adds tasks.project_id for existing databases and
        creates a default project (id/name are configurable via env).

        Idempotent: checks PRAGMA table_info before running ALTER.
        """
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        version = int(row["value"]) if row else 1
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "project_id" not in cols:
            # old pre-v2 database — add the column with a default
            default_id = os.environ.get("KANBAN_DEFAULT_PROJECT_ID", "default")
            self._conn.execute(
                f"ALTER TABLE tasks ADD COLUMN project_id TEXT NOT NULL DEFAULT '{default_id}'"
            )
        # The project_id index is always created (idempotent). For a fresh
        # database the column appeared from CREATE TABLE in schema.sql; for
        # older databases it is created after the ALTER above.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_project_status "
            "ON tasks(project_id, status, column_order)"
        )
        # The default project is created ONLY when the database has no
        # projects at all (fresh install). Existing databases keep their
        # own projects without an extra "default" being added on top.
        row = self._conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        if row["n"] == 0:
            default_id = os.environ.get("KANBAN_DEFAULT_PROJECT_ID", "default")
            default_name = os.environ.get("KANBAN_DEFAULT_PROJECT_NAME", "Default")
            default_color = os.environ.get("KANBAN_DEFAULT_PROJECT_COLOR", "#F10D30")
            default_icon = os.environ.get(
                "KANBAN_DEFAULT_PROJECT_ICON", default_name[:1].upper()
            )
            self._conn.execute(
                "INSERT INTO projects (id, name, color, icon, sort_order, archived, created_at) "
                "VALUES (?, ?, ?, ?, 0, 0, ?)",
                (default_id, default_name, default_color, default_icon, _now()),
            )
        if version < 2:
            self._conn.execute(
                "UPDATE meta SET value='2' WHERE key='schema_version'"
            )

    # ------------------------------------------------------------------
    # ID generation
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='next_id'"
            ).fetchone()
            n = int(row["value"]) if row else 1
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='next_id'", (str(n + 1),)
            )
            return f"T-{n:03d}"

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_tasks(
        self,
        status: str | Iterable[str] | None = None,
        assignee: str | None = None,
        project_id: str | None = None,
    ) -> list[Task]:
        """List of tasks with filters, sorted by (status, column_order).

        ``project_id=None`` means "all projects". The board UI always
        passes a concrete project_id.
        """
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if status is not None:
            if isinstance(status, str):
                statuses = [status]
            else:
                statuses = list(status)
            placeholders = ",".join("?" * len(statuses))
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        if assignee is not None:
            sql += " AND assignee = ?"
            params.append(assignee)
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY status, column_order, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_task(r, eager_links=True) for r in rows]

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_task(row, eager_links=True, eager_history=True)

    def board(self) -> dict[str, list[Task]]:
        """Group tasks by status, in column order."""
        result: dict[str, list[Task]] = {s: [] for s in STATUSES}
        for t in self.list_tasks():
            result.setdefault(t.status, []).append(t)
        return result

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create_task(
        self,
        title: str,
        *,
        status: str = "backlog",
        priority: str = "normal",
        size: str = "M",
        description: str = "",
        acceptance: str = "",
        assignee: str | None = None,
        external_blocker: str | None = None,
        actor: str = "user",
        links: list[dict[str, str]] | None = None,
        task_id: str | None = None,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> Task:
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status}")
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                tid = task_id or self._next_id()
                # column_order — last in the column + 1 (per project)
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(column_order), -1) AS m FROM tasks "
                    "WHERE status=? AND project_id=?",
                    (status, project_id),
                ).fetchone()
                col_order = (row["m"] + 1) if row else 0
                self._conn.execute(
                    """
                    INSERT INTO tasks (id, title, status, priority, size, assignee,
                                        description, acceptance, external_blocker,
                                        created_at, moved_at, column_order, project_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tid,
                        title,
                        status,
                        priority,
                        size,
                        assignee,
                        description,
                        acceptance,
                        external_blocker,
                        ts,
                        ts,
                        col_order,
                        project_id,
                    ),
                )
                if links:
                    for ln in links:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO task_links (task_id, type, value) VALUES (?, ?, ?)",
                            (tid, ln["type"], ln["value"]),
                        )
                self._conn.execute(
                    """
                    INSERT INTO task_history (task_id, ts, actor, action, from_status, to_status, comment)
                    VALUES (?, ?, ?, 'create', NULL, ?, ?)
                    """,
                    (tid, ts, actor, status, None),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        task = self.get_task(tid)
        assert task is not None
        return task

    def move_task(
        self,
        task_id: str,
        to_status: str,
        *,
        actor: str = "user",
        comment: str | None = None,
        column_order: int | None = None,
    ) -> Task:
        if to_status not in STATUSES:
            raise ValueError(f"unknown status: {to_status}")
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                row = self._conn.execute(
                    "SELECT status, project_id FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                from_status = row["status"]
                project_id = row["project_id"]
                # column_order — append to the end of the project's column when not specified
                if column_order is None:
                    r2 = self._conn.execute(
                        "SELECT COALESCE(MAX(column_order), -1) AS m FROM tasks "
                        "WHERE status=? AND project_id=?",
                        (to_status, project_id),
                    ).fetchone()
                    column_order = (r2["m"] + 1) if r2 else 0
                self._conn.execute(
                    """UPDATE tasks SET status=?, moved_at=?, column_order=?
                       WHERE id=?""",
                    (to_status, ts, column_order, task_id),
                )
                self._conn.execute(
                    """INSERT INTO task_history
                       (task_id, ts, actor, action, from_status, to_status, comment)
                       VALUES (?, ?, ?, 'move', ?, ?, ?)""",
                    (task_id, ts, actor, from_status, to_status, comment),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        task = self.get_task(task_id)
        assert task is not None
        return task

    def assign_task(self, task_id: str, assignee: str | None, *, actor: str) -> Task:
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                row = self._conn.execute(
                    "SELECT assignee FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                self._conn.execute(
                    "UPDATE tasks SET assignee=? WHERE id=?", (assignee, task_id)
                )
                self._conn.execute(
                    """INSERT INTO task_history
                       (task_id, ts, actor, action, from_status, to_status, comment)
                       VALUES (?, ?, ?, 'assign', NULL, NULL, ?)""",
                    (task_id, ts, actor, f"assignee → {assignee}"),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id)
        assert t is not None
        return t

    def pull_task(self, task_id: str, assignee: str = "claude") -> Task:
        """Atomic: assignee IS NULL → assignee, status approved → analyst.

        Used by Claude/an agent for a safe "claim the task" operation.
        """
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                row = self._conn.execute(
                    "SELECT assignee, status, project_id FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                if row["assignee"] is not None and row["assignee"] != assignee:
                    raise RuntimeError(
                        f"task {task_id} already assigned to {row['assignee']}"
                    )
                if row["status"] != "approved":
                    raise RuntimeError(
                        f"task {task_id} is in '{row['status']}', not 'approved'"
                    )
                # move to analyst and claim (per project)
                r2 = self._conn.execute(
                    "SELECT COALESCE(MAX(column_order), -1) AS m FROM tasks "
                    "WHERE status='analyst' AND project_id=?",
                    (row["project_id"],),
                ).fetchone()
                col_order = (r2["m"] + 1) if r2 else 0
                self._conn.execute(
                    """UPDATE tasks SET status='analyst', assignee=?, moved_at=?, column_order=?
                       WHERE id=?""",
                    (assignee, ts, col_order, task_id),
                )
                self._conn.execute(
                    """INSERT INTO task_history
                       (task_id, ts, actor, action, from_status, to_status, comment)
                       VALUES (?, ?, ?, 'move', 'approved', 'analyst', 'pulled')""",
                    (task_id, ts, assignee),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id)
        assert t is not None
        return t

    def add_comment(self, task_id: str, text: str, *, actor: str) -> None:
        ts = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError(task_id)
            self._conn.execute(
                """INSERT INTO task_history
                   (task_id, ts, actor, action, from_status, to_status, comment)
                   VALUES (?, ?, ?, 'comment', NULL, NULL, ?)""",
                (task_id, ts, actor, text),
            )

    def add_link(self, task_id: str, type_: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO task_links (task_id, type, value) VALUES (?, ?, ?)",
                (task_id, type_, value),
            )

    def set_blockers(self, task_id: str, blocker_ids: list[str]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "DELETE FROM task_blockers WHERE task_id=?", (task_id,)
                )
                for b in blocker_ids:
                    self._conn.execute(
                        "INSERT INTO task_blockers (task_id, blocker_id) VALUES (?, ?)",
                        (task_id, b),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def update_fields(
        self,
        task_id: str,
        *,
        actor: str,
        title: str | None = None,
        priority: str | None = None,
        size: str | None = None,
        description: str | None = None,
        acceptance: str | None = None,
        external_blocker: str | None = None,
    ) -> Task:
        ts = _now()
        sets: list[str] = []
        params: list[Any] = []
        for col, val in (
            ("title", title),
            ("priority", priority),
            ("size", size),
            ("description", description),
            ("acceptance", acceptance),
            ("external_blocker", external_blocker),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        if not sets:
            t = self.get_task(task_id)
            if t is None:
                raise KeyError(task_id)
            return t
        params.append(task_id)
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                row = self._conn.execute(
                    "SELECT 1 FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                self._conn.execute(
                    f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params
                )
                self._conn.execute(
                    """INSERT INTO task_history
                       (task_id, ts, actor, action, from_status, to_status, comment)
                       VALUES (?, ?, ?, 'update', NULL, NULL, ?)""",
                    (task_id, ts, actor, ", ".join(s.split(" = ")[0] for s in sets)),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id)
        assert t is not None
        return t

    def reorder(self, task_id: str, new_order: int) -> None:
        """Change the order within the current column."""
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET column_order=? WHERE id=?", (new_order, task_id)
            )

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        """All projects with task_counts aggregated by status."""
        sql = "SELECT * FROM projects"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY sort_order, name"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
            counts_rows = self._conn.execute(
                "SELECT project_id, status, COUNT(*) AS n "
                "FROM tasks GROUP BY project_id, status"
            ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        totals: dict[str, int] = {}
        for r in counts_rows:
            counts.setdefault(r["project_id"], {})[r["status"]] = r["n"]
            totals[r["project_id"]] = totals.get(r["project_id"], 0) + r["n"]
        result = []
        for r in rows:
            p = self._row_to_project(r)
            p.task_counts = counts.get(p.id, {})
            p.total_tasks = totals.get(p.id, 0)
            result.append(p)
        return result

    def get_project(self, project_id: str) -> Project | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_project(row)

    def create_project(
        self,
        project_id: str,
        name: str,
        *,
        color: str = "#F10D30",
        icon: str = "",
        sort_order: int | None = None,
        path: str | None = None,
    ) -> Project:
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                if sort_order is None:
                    r = self._conn.execute(
                        "SELECT COALESCE(MAX(sort_order), -1) AS m FROM projects"
                    ).fetchone()
                    sort_order = (r["m"] + 1) if r else 0
                self._conn.execute(
                    """INSERT INTO projects
                       (id, name, color, icon, sort_order, archived, path, created_at)
                       VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                    (project_id, name, color, icon or name[:1].upper(), sort_order, path, ts),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        p = self.get_project(project_id)
        assert p is not None
        return p

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        color: str | None = None,
        icon: str | None = None,
        sort_order: int | None = None,
        path: str | None = None,
    ) -> Project:
        sets: list[str] = []
        params: list[Any] = []
        # path is forwarded as-is (None means "leave alone", "" means "clear").
        for col, val in (
            ("name", name), ("color", color), ("icon", icon),
            ("sort_order", sort_order), ("path", path),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                # an empty string for path becomes NULL in the database
                params.append(None if (col == "path" and val == "") else val)
        if not sets:
            p = self.get_project(project_id)
            if p is None:
                raise KeyError(project_id)
            return p
        params.append(project_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if not row:
                raise KeyError(project_id)
            self._conn.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE id=?", params
            )
        p = self.get_project(project_id)
        assert p is not None
        return p

    # ------------------------------------------------------------------
    # Project sources (one source per project)
    # ------------------------------------------------------------------

    def set_project_source(
        self, project_id: str, type_: str, config: dict[str, Any]
    ) -> None:
        ts = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO project_sources (project_id, type, config, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(project_id) DO UPDATE
                   SET type=excluded.type, config=excluded.config""",
                (project_id, type_, json.dumps(config, ensure_ascii=False), ts),
            )

    def get_project_source(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM project_sources WHERE project_id=?", (project_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "project_id": row["project_id"],
            "type": row["type"],
            "config": json.loads(row["config"]),
            "last_sync_at": row["last_sync_at"],
            "created_at": row["created_at"],
        }

    def update_source_sync_time(self, project_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE project_sources SET last_sync_at=? WHERE project_id=?",
                (_now(), project_id),
            )

    def delete_project_source(self, project_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM project_sources WHERE project_id=?", (project_id,)
            )

    def archive_project(self, project_id: str, archived: bool = True) -> Project:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if not row:
                raise KeyError(project_id)
            self._conn.execute(
                "UPDATE projects SET archived=? WHERE id=?",
                (1 if archived else 0, project_id),
            )
        p = self.get_project(project_id)
        assert p is not None
        return p

    def _row_to_project(self, row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            color=row["color"],
            icon=row["icon"],
            sort_order=row["sort_order"],
            archived=bool(row["archived"]),
            created_at=row["created_at"],
            path=row["path"] if "path" in row.keys() else None,
        )

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Dump the whole board as a plain dict for JSON persistence."""
        tasks = self.list_tasks()
        projects = self.list_projects(include_archived=True)
        return {
            "exported_at": _now(),
            "schema_version": 2,
            "projects": [p.to_public() for p in projects],
            "tasks": [t.to_public() for t in tasks],
        }

    def save_snapshot(self, dest_dir: str | Path | None = None) -> Path:
        if dest_dir is None:
            dest_dir = Path(__file__).resolve().parent.parent / "snapshots"
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fp = dest_dir / f"{date_part}.json"
        fp.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return fp

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _row_to_task(
        self,
        row: sqlite3.Row,
        *,
        eager_links: bool = False,
        eager_history: bool = False,
    ) -> Task:
        t = Task(
            id=row["id"],
            title=row["title"],
            status=row["status"],
            priority=row["priority"],
            size=row["size"],
            assignee=row["assignee"],
            description=row["description"],
            acceptance=row["acceptance"],
            external_blocker=row["external_blocker"],
            created_at=row["created_at"],
            moved_at=row["moved_at"],
            column_order=row["column_order"],
            project_id=row["project_id"] if "project_id" in row.keys() else DEFAULT_PROJECT_ID,
        )
        if eager_links:
            link_rows = self._conn.execute(
                "SELECT type, value FROM task_links WHERE task_id=? ORDER BY type, value",
                (t.id,),
            ).fetchall()
            t.links = [{"type": r["type"], "value": r["value"]} for r in link_rows]
            blocker_rows = self._conn.execute(
                "SELECT blocker_id FROM task_blockers WHERE task_id=?", (t.id,)
            ).fetchall()
            t.blockers = [r["blocker_id"] for r in blocker_rows]
        if eager_history:
            h_rows = self._conn.execute(
                "SELECT * FROM task_history WHERE task_id=? ORDER BY ts ASC, id ASC",
                (t.id,),
            ).fetchall()
            t.history = [
                TaskHistory(
                    id=r["id"],
                    task_id=r["task_id"],
                    ts=r["ts"],
                    actor=r["actor"],
                    action=r["action"],
                    from_status=r["from_status"],
                    to_status=r["to_status"],
                    comment=r["comment"],
                )
                for r in h_rows
            ]
        return t

    def close(self) -> None:
        with self._lock:
            self._conn.close()

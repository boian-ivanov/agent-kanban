"""FastAPI app for the kanban board. Every mutation is recorded in history
with actor=KANBAN_ACTOR env var (default 'user').

Endpoints (v2):
    GET    /                          — HTML (root, default project)
    GET    /p/{project_id}            — HTML for a specific project
    GET    /api/board?project=&include=  — tasks of a project + column meta (include=full → full payloads)
    GET    /api/projects              — list of projects with task_counts
    POST   /api/projects              — create a project
    PATCH  /api/projects/{id}         — update (name/color/icon/sort_order)
    POST   /api/projects/{id}/archive — archive (toggle)
    GET    /api/tasks?project=&status=&assignee=&parent_id=&updated_since=  — filtered task list
    GET    /api/tasks/{task_id}       — full card with history
    POST   /api/tasks                 — create (project_id in payload)
    PATCH  /api/tasks/{task_id}       — update fields
    POST   /api/tasks/{task_id}/move  — drag-drop result
    POST   /api/tasks/{task_id}/comment
    POST   /api/tasks/{task_id}/links
    POST   /api/tasks/{task_id}/blockers
    POST   /api/snapshot              — persist a board snapshot
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from kanban_store import Store, STATUSES, status_meta
from kanban_store.store import DEFAULT_PROJECT_ID
from kanban_ui.automation import (
    InboxWatcher,
    RuleEngine,
    inbox_status,
    rules_status,
    emit_rule_event,
    init_dispatcher,
    shutdown_dispatcher,
    emit_event,
    webhooks_status,
    plan_md,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("kanban.ui")


def _actor() -> str:
    """Name of the mutation author recorded in task_history.

    Pulled from the ``KANBAN_ACTOR`` env variable; defaults to ``user``.
    Suitable for single-user installations; for multi-user setups replace
    this with a cookie/header lookup.
    """
    return os.environ.get("KANBAN_ACTOR", "user")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
ROOT = Path(__file__).resolve().parent.parent
KANBAN_DATA = ROOT / "kanban_data"

_store = Store()


def _inbox_dir() -> Path:
    return Path(os.environ.get("KANBAN_INBOX_DIR") or (KANBAN_DATA / "inbox"))


def _rules_file() -> Path:
    return Path(os.environ.get("KANBAN_RULES_FILE") or (KANBAN_DATA / "rules.json"))


def _webhooks_file() -> Path:
    return Path(
        os.environ.get("KANBAN_WEBHOOKS_FILE") or (KANBAN_DATA / "webhooks.json")
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Starts background tasks: inbox watcher + rule engine + webhook dispatcher."""
    KANBAN_DATA.mkdir(parents=True, exist_ok=True)
    inbox = InboxWatcher(_store, _inbox_dir())
    engine = RuleEngine(_store, _rules_file())
    init_dispatcher(_webhooks_file())
    inbox_task = asyncio.create_task(inbox.run(), name="inbox-watcher")
    rules_task = asyncio.create_task(engine.run(), name="rule-engine")
    log.info(
        "automation: inbox=%s rules=%s webhooks=%s",
        _inbox_dir(), _rules_file(), _webhooks_file(),
    )
    try:
        yield
    finally:
        inbox.stop()
        engine.stop()
        await asyncio.gather(inbox_task, rules_task, return_exceptions=True)
        await shutdown_dispatcher()


app = FastAPI(title="Kanban", version="2.0", lifespan=lifespan)

# Optional CORS — for remote agents (e.g. Open WebUI on a different host).
# Disabled by default: the kanban listens on 127.0.0.1, no one should reach
# it cross-origin. Enable via env: KANBAN_CORS_ORIGINS=https://web.example,https://other
_cors_origins = os.environ.get("KANBAN_CORS_ORIGINS", "").strip()
if _cors_origins:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _cors_origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
PROJECT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{0,3}$")


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    acceptance: str = ""
    status: str = "backlog"
    priority: str = "normal"
    size: str = "M"
    external_blocker: str | None = None
    assignee: str | None = None
    links: list[dict[str, str]] = Field(default_factory=list)
    project_id: str = DEFAULT_PROJECT_ID


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    acceptance: str | None = None
    priority: str | None = None
    size: str | None = None
    external_blocker: str | None = None
    assignee: str | None = None


class MoveRequest(BaseModel):
    to_status: str
    column_order: int | None = None
    comment: str | None = None


class CommentRequest(BaseModel):
    text: str


class AssignRequest(BaseModel):
    assignee: str | None = None


class LinkRequest(BaseModel):
    type: str
    value: str


class BlockersRequest(BaseModel):
    blocker_ids: list[str]


class ProjectCreate(BaseModel):
    id: str = Field(..., description="slug: lowercase, a-z 0-9 -, 2-32 chars")
    name: str
    color: str = "#F10D30"
    icon: str = ""
    sort_order: int | None = None
    path: str | None = None
    model: str | None = None
    code: str | None = Field(
        None,
        description="ticket id prefix, uppercase 1-4 chars (e.g. AK, SP); "
        "None = legacy T-### ids",
    )


class ProjectUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    icon: str | None = None
    sort_order: int | None = None
    path: str | None = None
    model: str | None = None
    code: str | None = Field(
        None,
        description="ticket id prefix, uppercase 1-4 chars (e.g. AK, SP); "
        "None leaves it unchanged, \"\" clears back to legacy T-### ids",
    )


class ProjectArchiveRequest(BaseModel):
    archived: bool = True


class SourcePlanLocalRequest(BaseModel):
    files: list[str] = Field(
        default_factory=list,
        description="paths to plan files relative to project.path (multiple allowed)",
    )
    # Backward compat: a single file is accepted and lands in files[0].
    file: str | None = None


class SourceGitRequest(BaseModel):
    repo_url: str
    token: str = ""


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/p/{project_id}", response_class=HTMLResponse)
def index_for_project(project_id: str) -> HTMLResponse:
    """SPA-style: same HTML, the frontend reads project_id from location.pathname."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------


@app.get("/api/board")
def get_board(
    project: str = Query(DEFAULT_PROJECT_ID, description="project_id"),
    include: str = Query(
        "summary", description="summary|full — full returns the complete card payload"
    ),
) -> dict[str, Any]:
    """Board grouped by status.

    ``include=full`` returns full card payloads (description, acceptance,
    comments, ancestors, parent_id, ...) for every task instead of the
    compact summary fields.
    """
    columns = status_meta()
    proj = _store.get_project(project)
    if proj is None:
        raise HTTPException(404, f"project {project} not found")
    full = include == "full"
    by_status: dict[str, list[dict[str, Any]]] = {s: [] for s in STATUSES}
    for t in _store.list_tasks(project_id=project, eager_history=full, eager_tree=full):
        if full:
            by_status[t.status].append(t.to_public())
        else:
            by_status[t.status].append(
                {
                    "id": t.id,
                    "title": t.title,
                    "priority": t.priority,
                    "size": t.size,
                    "assignee": t.assignee,
                    "external_blocker": t.external_blocker,
                    "blockers": t.blockers,
                    "moved_at": t.moved_at,
                    "project_id": t.project_id,
                }
            )
    return {
        "columns": columns,
        "tasks": by_status,
        "project": proj.to_public(),
    }


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@app.get("/api/projects")
def list_projects(include_archived: bool = False) -> dict[str, Any]:
    projects = _store.list_projects(include_archived=include_archived)
    return {"projects": [p.to_public() for p in projects]}


def _normalize_path(p: str | None) -> str | None:
    """Normalize a project path: ``~`` → home, absolute path.

    Returns None if ``p`` is an empty string or None.
    Does not validate existence — the user may point at a directory that
    has not been created yet or a planned path.
    """
    if p is None:
        return None
    p = p.strip()
    if not p:
        return None
    return os.path.abspath(os.path.expanduser(p))


def _normalize_code(code: str | None) -> str | None:
    """Normalize a project code: trim, uppercase; None/"" → None (legacy)."""
    if not code:
        return None
    return code.strip().upper()


@app.post("/api/projects", status_code=201)
def create_project(req: ProjectCreate) -> dict[str, Any]:
    if not PROJECT_ID_RE.match(req.id):
        raise HTTPException(
            400, "project id must match ^[a-z][a-z0-9-]{1,31}$ (lowercase slug)"
        )
    if _store.get_project(req.id) is not None:
        raise HTTPException(409, f"project {req.id} already exists")
    code = _normalize_code(req.code)
    if code is not None and not PROJECT_CODE_RE.match(code):
        raise HTTPException(
            400, "project code must match ^[A-Z][A-Z0-9]{0,3}$ (uppercase, 1-4 chars)"
        )
    try:
        p = _store.create_project(
            req.id,
            req.name,
            color=req.color,
            icon=req.icon,
            sort_order=req.sort_order,
            path=_normalize_path(req.path),
            model=req.model or None,
            code=code,
        )
    except ValueError as e:
        raise HTTPException(409, str(e))
    return p.to_public()


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, req: ProjectUpdate) -> dict[str, Any]:
    code = _normalize_code(req.code)
    if code is not None and not PROJECT_CODE_RE.match(code):
        raise HTTPException(
            400, "project code must match ^[A-Z][A-Z0-9]{0,3}$ (uppercase, 1-4 chars)"
        )
    try:
        p = _store.update_project(
            project_id,
            name=req.name,
            color=req.color,
            icon=req.icon,
            sort_order=req.sort_order,
            # an empty string clears path/model in the database
            path=_normalize_path(req.path) if req.path != "" else "",
            model=req.model or "",
            code=code,
        )
    except KeyError:
        raise HTTPException(404, f"project {project_id} not found")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return p.to_public()


def _project_path_or_400(project_id: str) -> Path:
    """Returns project.path or raises 400 if it is not set."""
    p = _store.get_project(project_id)
    if p is None:
        raise HTTPException(404, f"project {project_id} not found")
    if not p.path:
        raise HTTPException(
            400, "project has no path — set a directory via PATCH /api/projects/{id}"
        )
    return Path(p.path)


def _save_git_secret(project_id: str, token: str) -> None:
    """Stores the token in kanban_data/.env-secrets with mode 600."""
    secrets_file = KANBAN_DATA / ".env-secrets"
    KANBAN_DATA.mkdir(parents=True, exist_ok=True)
    var = f"KANBAN_GIT_TOKEN_{project_id.upper().replace('-','_')}"
    lines: list[str] = []
    if secrets_file.exists():
        for line in secrets_file.read_text(encoding="utf-8").splitlines():
            if not line.startswith(var + "="):
                lines.append(line)
    if token:
        lines.append(f"{var}={token}")
    secrets_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(secrets_file, 0o600)
    except OSError:
        pass


_PRIO_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    # Lower number = higher priority. Plan files come first.
    (re.compile(r"^(project[-_]?)?plan\b", re.I), 1),
    (re.compile(r"^backlog\b", re.I), 2),
    (re.compile(r"^tasks?\b", re.I), 3),
    (re.compile(r"^todo\b", re.I), 3),
    (re.compile(r"^roadmap\b", re.I), 4),
    (re.compile(r"\b(plan|backlog|tasks?|todo|roadmap)\b", re.I), 5),  # appears anywhere in the name
    (re.compile(r"^claude\b", re.I), 7),
    (re.compile(r"^agents?\b", re.I), 8),
    (re.compile(r"^readme\b", re.I), 9),
]


def _file_prio(filename: str, in_root: bool) -> int:
    name_no_ext = filename.rsplit(".", 1)[0]
    for pat, prio in _PRIO_PATTERNS:
        if pat.search(name_no_ext):
            return prio if in_root else prio + 30
    return 50 if in_root else 80


def _scan_md_candidates(root: Path) -> list[dict[str, Any]]:
    """Scan a directory and its subdirs for ``*.md`` files that look like
    a "plan file". Returns a sorted list prioritising PLAN/BACKLOG/TASKS/
    TODO/ROADMAP — including names like ``PROJECT-PLAN.md`` or
    ``my-plan-2026.md`` (regex on the basename).
    """
    SEARCH_DIRS = ["", "docs", "notes", "plans", ".claude", "tasks"]
    out: list[dict[str, Any]] = []
    for sub in SEARCH_DIRS:
        d = root / sub if sub else root
        if not d.exists() or not d.is_dir():
            continue
        try:
            for entry in d.iterdir():
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in (".md", ".markdown"):
                    continue
                if entry.name.startswith("."):
                    continue
                rel = str(entry.relative_to(root))
                stat = entry.stat()
                prio = _file_prio(entry.name, in_root=(sub == ""))
                out.append({
                    "file": rel,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "prio": prio,
                })
        except (OSError, PermissionError):
            continue
    out.sort(key=lambda x: (x["prio"], -x["modified"]))
    return out[:30]


@app.get("/api/projects/{project_id}/plan-candidates")
def list_plan_candidates(project_id: str) -> dict[str, Any]:
    """Plan-file candidates for an existing project (uses project.path)."""
    p = _store.get_project(project_id)
    if p is None:
        raise HTTPException(404, f"project {project_id} not found")
    if not p.path:
        return {"items": [], "reason": "project has no path"}
    root = Path(p.path)
    if not root.exists():
        return {"items": [], "reason": f"path does not exist: {root}"}
    return {"items": _scan_md_candidates(root), "root": str(root)}


@app.get("/api/system/list-md-files")
def list_md_files(path: str) -> dict[str, Any]:
    """Plan-file candidates for an arbitrary path (used by the wizard before the project is created)."""
    abs_path = os.path.abspath(os.path.expanduser(path.strip()))
    root = Path(abs_path)
    if not root.exists() or not root.is_dir():
        return {"items": [], "reason": "directory not found"}
    return {"items": _scan_md_candidates(root), "root": str(root)}


@app.get("/api/projects/{project_id}/source")
def get_project_source(project_id: str) -> dict[str, Any]:
    src = _store.get_project_source(project_id)
    if src is None:
        raise HTTPException(404, f"no source configured for {project_id}")
    return src


@app.delete("/api/projects/{project_id}/source")
def delete_project_source(project_id: str) -> dict[str, Any]:
    _store.delete_project_source(project_id)
    return {"ok": True}


@app.post("/api/projects/{project_id}/source/plan-new", status_code=201)
def setup_source_plan_new(project_id: str) -> dict[str, Any]:
    """Creates PLAN.md in project.path, updates CLAUDE.md, registers the source."""
    p = _store.get_project(project_id)
    if p is None:
        raise HTTPException(404, f"project {project_id} not found")
    project_dir = _project_path_or_400(project_id)
    if not project_dir.exists():
        raise HTTPException(
            400, f"directory does not exist: {project_dir}"
        )
    plan_path = plan_md.init_plan_md(project_dir, p.id, p.name)
    plan_md.update_claude_md(
        project_dir / "CLAUDE.md", p.id, p.name, plan_relative="PLAN.md"
    )
    _store.set_project_source(
        project_id, "plan_md", {"file": "PLAN.md", "absolute": str(plan_path)}
    )
    return {
        "ok": True,
        "plan_md": str(plan_path),
        "claude_md": str(project_dir / "CLAUDE.md"),
        "source": _store.get_project_source(project_id),
    }


@app.post("/api/projects/{project_id}/source/plan-local", status_code=201)
def setup_source_plan_local(
    project_id: str, req: SourcePlanLocalRequest
) -> dict[str, Any]:
    """Connect one or more plan files, import tasks from all of them,
    and update CLAUDE.md (mentioning the first file as the canonical one).
    """
    p = _store.get_project(project_id)
    if p is None:
        raise HTTPException(404, f"project {project_id} not found")
    project_dir = _project_path_or_400(project_id)
    files = list(req.files or [])
    if req.file and req.file.strip():
        files.append(req.file.strip())
    files = [f.strip().lstrip("/") for f in files if f.strip()]
    if not files:
        raise HTTPException(400, "specify at least one file in `files`")

    resolved: list[Path] = []
    for rel in files:
        pth = (project_dir / rel).resolve()
        try:
            pth.relative_to(project_dir.resolve())
        except ValueError:
            raise HTTPException(400, f"file must be inside project directory: {rel}")
        if not pth.exists():
            raise HTTPException(400, f"plan file does not exist: {rel}")
        resolved.append(pth)

    total_created = 0
    total_skipped = 0
    per_file: list[dict[str, Any]] = []
    for rel, pth in zip(files, resolved):
        counts = plan_md.import_plan_md(_store, project_id, pth)
        per_file.append({"file": rel, **counts})
        total_created += counts["created"]
        total_skipped += counts["skipped"]
    # CLAUDE.md highlights the first file as the canonical "write new tasks here" target.
    plan_md.update_claude_md(
        project_dir / "CLAUDE.md", p.id, p.name, plan_relative=files[0]
    )
    _store.set_project_source(
        project_id,
        "plan_md",
        {
            "files": files,
            "absolute": [str(x) for x in resolved],
        },
    )
    _store.update_source_sync_time(project_id)
    return {
        "ok": True,
        "imported": {"created": total_created, "skipped": total_skipped},
        "per_file": per_file,
        "source": _store.get_project_source(project_id),
    }


@app.post("/api/projects/{project_id}/source/git", status_code=201)
def setup_source_git(project_id: str, req: SourceGitRequest) -> dict[str, Any]:
    """Registers a git source: URL in config, token in .env-secrets (0600)."""
    if _store.get_project(project_id) is None:
        raise HTTPException(404, f"project {project_id} not found")
    if not req.repo_url.strip():
        raise HTTPException(400, "repo_url required")
    _store.set_project_source(
        project_id, "git", {"repo_url": req.repo_url.strip()}
    )
    if req.token:
        _save_git_secret(project_id, req.token)
    return {
        "ok": True,
        "source": _store.get_project_source(project_id),
        "secret_var": f"KANBAN_GIT_TOKEN_{project_id.upper().replace('-','_')}",
        "note": "Issue import will be added in a future release.",
    }


@app.post("/api/projects/{project_id}/archive")
def archive_project(project_id: str, req: ProjectArchiveRequest) -> dict[str, Any]:
    if project_id == DEFAULT_PROJECT_ID and req.archived:
        raise HTTPException(400, f"cannot archive default project '{DEFAULT_PROJECT_ID}'")
    try:
        p = _store.archive_project(project_id, archived=req.archived)
    except KeyError:
        raise HTTPException(404, f"project {project_id} not found")
    return p.to_public()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@app.get("/api/tasks")
def list_tasks_api(
    project: str | None = Query(None, description="project_id filter"),
    status: str | None = Query(None, description="status filter"),
    assignee: str | None = Query(None, description="assignee filter"),
    parent_id: str | None = Query(None, description="parent_id filter"),
    updated_since: str | None = Query(
        None,
        description="ISO8601 — only tasks with a history entry at or after this timestamp",
    ),
) -> dict[str, Any]:
    """Filterable task list (summary cards), sorted by status/column_order.

    Replaces per-task fetching for agents: project/status/assignee/parent_id
    and last-updated filters in a single request.
    """
    tasks = _store.list_tasks(
        status=status,
        assignee=assignee,
        project_id=project,
        parent_id=parent_id,
        updated_since=updated_since,
    )
    return {
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "size": t.size,
                "assignee": t.assignee,
                "external_blocker": t.external_blocker,
                "blockers": t.blockers,
                "parent_id": t.parent_id,
                "kind": t.kind,
                "moved_at": t.moved_at,
                "created_at": t.created_at,
                "project_id": t.project_id,
            }
            for t in tasks
        ],
        "count": len(tasks),
    }


# ---------------------------------------------------------------------------
# Single task
# ---------------------------------------------------------------------------


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    t = _store.get_task(task_id)
    if not t:
        raise HTTPException(404, f"task {task_id} not found")
    return t.to_public()


def _project_payload(project_id: str | None) -> dict[str, Any] | None:
    if not project_id:
        return None
    p = _store.get_project(project_id)
    return p.to_public() if p else None


@app.post("/api/tasks", status_code=201)
async def create_task(req: TaskCreate) -> dict[str, Any]:
    if req.status not in STATUSES:
        raise HTTPException(400, f"unknown status: {req.status}")
    if _store.get_project(req.project_id) is None:
        raise HTTPException(400, f"unknown project: {req.project_id}")
    t = _store.create_task(
        title=req.title,
        description=req.description,
        acceptance=req.acceptance,
        status=req.status,
        priority=req.priority,
        size=req.size,
        external_blocker=req.external_blocker,
        assignee=req.assignee,
        actor=_actor(),
        links=req.links or None,
        project_id=req.project_id,
    )
    await emit_event("task_created", {
        "task": t.to_public(),
        "project": _project_payload(t.project_id),
    })
    return t.to_public()


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: str, req: TaskUpdate) -> dict[str, Any]:
    try:
        t = _store.update_fields(
            task_id,
            actor=_actor(),
            title=req.title,
            description=req.description,
            acceptance=req.acceptance,
            priority=req.priority,
            size=req.size,
            external_blocker=req.external_blocker,
        )
        if "assignee" in req.model_fields_set and t.assignee != req.assignee:
            t = _store.assign_task(task_id, req.assignee, actor=_actor())
    except KeyError:
        raise HTTPException(404, f"task {task_id} not found")
    changed = [
        f for f, v in (
            ("title", req.title), ("description", req.description),
            ("acceptance", req.acceptance), ("priority", req.priority),
            ("size", req.size), ("external_blocker", req.external_blocker),
            ("assignee", req.assignee),
        ) if v is not None
    ]
    await emit_event("task_updated", {
        "task": t.to_public(),
        "project": _project_payload(t.project_id),
        "changed_fields": changed,
    })
    return t.to_public()


@app.post("/api/tasks/{task_id}/move")
async def move_task(task_id: str, req: MoveRequest) -> dict[str, Any]:
    if req.to_status not in STATUSES:
        raise HTTPException(400, f"unknown status: {req.to_status}")
    pre = _store.get_task(task_id)
    from_status = pre.status if pre else None
    try:
        t = _store.move_task(
            task_id,
            req.to_status,
            actor=_actor(),
            comment=req.comment,
            column_order=req.column_order,
        )
    except KeyError:
        raise HTTPException(404, f"task {task_id} not found")
    if from_status != req.to_status:    # only emit on an actual move
        payload = {
            "task": t.to_public(),
            "project": _project_payload(t.project_id),
            "from_status": from_status,
            "to_status": req.to_status,
            "comment": req.comment,
        }
        await emit_event("task_moved", payload)
        emit_rule_event("task_moved", payload)
    return t.to_public()


@app.post("/api/tasks/{task_id}/assign")
async def assign_task(task_id: str, req: AssignRequest) -> dict[str, Any]:
    pre = _store.get_task(task_id)
    if pre is None:
        raise HTTPException(404, f"task {task_id} not found")
    if pre.assignee == req.assignee:
        return pre.to_public()
    try:
        t = _store.assign_task(task_id, req.assignee, actor=_actor())
    except KeyError:
        raise HTTPException(404, f"task {task_id} not found")
    await emit_event("task_updated", {
        "task": t.to_public(),
        "project": _project_payload(t.project_id),
        "changed_fields": ["assignee"],
    })
    return t.to_public()


@app.post("/api/tasks/{task_id}/comment", status_code=201)
async def add_comment(task_id: str, req: CommentRequest) -> dict[str, Any]:
    try:
        _store.add_comment(task_id, req.text, actor=_actor())
    except KeyError:
        raise HTTPException(404, f"task {task_id} not found")
    t = _store.get_task(task_id)
    if t:
        await emit_event("task_commented", {
            "task": t.to_public(),
            "project": _project_payload(t.project_id),
            "comment": req.text,
        })
    return {"ok": True}


@app.post("/api/tasks/{task_id}/links", status_code=201)
def add_link(task_id: str, req: LinkRequest) -> dict[str, Any]:
    if req.type not in {"memory", "file", "pr", "url"}:
        raise HTTPException(400, "type must be memory/file/pr/url")
    _store.add_link(task_id, req.type, req.value)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/blockers")
def set_blockers(task_id: str, req: BlockersRequest) -> dict[str, Any]:
    _store.set_blockers(task_id, req.blocker_ids)
    return {"ok": True, "blockers": req.blocker_ids}


# ---------------------------------------------------------------------------
# Agent log streaming (SSE)
# ---------------------------------------------------------------------------

AGENT_LOG_DIR = KANBAN_DATA / "agent-logs"
AGENT_LOG_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/api/tasks/{task_id}/log/stream")
async def stream_task_log(task_id: str):
    """SSE endpoint: streams the agent log file for a task.

    Sends existing content immediately, then polls for new lines every 500ms.
    Closes when the log file is deleted (agent cleanup) or the client disconnects.
    """
    log_file = AGENT_LOG_DIR / f"{task_id}.log"

    async def event_generator():
        last_size = 0

        # Send any existing content first
        if log_file.exists():
            try:
                content = log_file.read_text(encoding="utf-8", errors="replace")
                if content:
                    last_size = len(content.encode("utf-8"))
                    yield f"data: {json.dumps({'lines': content})}\n\n"
            except OSError:
                pass

        # Poll for new content
        while True:
            await asyncio.sleep(0.5)
            if not log_file.exists():
                yield f"data: {json.dumps({'done': True})}\n\n"
                return
            try:
                current_size = log_file.stat().st_size
                if current_size > last_size:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_size)
                        new_content = f.read()
                        if new_content:
                            last_size = f.tell()
                            yield f"data: {json.dumps({'lines': new_content})}\n\n"
            except OSError:
                yield f"data: {json.dumps({'done': True})}\n\n"
                return

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@app.post("/api/snapshot")
def snapshot() -> dict[str, Any]:
    fp = _store.save_snapshot()
    return {"ok": True, "path": str(fp)}


# ---------------------------------------------------------------------------
# Automation status
# ---------------------------------------------------------------------------


@app.get("/api/automation/status")
def get_automation_status() -> dict[str, Any]:
    """State of the inbox watcher, rule engine, and webhook dispatcher."""
    return {
        "inbox": inbox_status(),
        "rules": rules_status(),
        "webhooks": webhooks_status(),
    }


# ---------------------------------------------------------------------------
# Native folder picker (via the system dialog).
# Local-installation only — we open the native dialog from the server
# process (osascript / zenity / PowerShell), not through the browser.
# ---------------------------------------------------------------------------

_OSASCRIPT_PICK = """\
tell application "Finder" to activate
delay 0.1
try
  set f to choose folder with prompt "Choose the project directory"
  POSIX path of f
on error
  return ""
end try
"""


# ---------------------------------------------------------------------------
# Claude Code auth helpers
# ---------------------------------------------------------------------------


def _resolve_claude_bin() -> str | None:
    """Same logic as in examples/agent-launcher/launch-claude.sh.

    1) env ``CLAUDE_BIN``
    2) ``shutil.which`` on PATH (with an extended default PATH)
    3) macOS fallback: ~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude
    """
    env_bin = os.environ.get("CLAUDE_BIN")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    # widen PATH: a launchd process may have a narrow PATH
    extra = ":".join([
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.npm-global/bin"),
        "/opt/homebrew/bin",
    ])
    candidates = (extra + ":" + os.environ.get("PATH", "")).split(":")
    for d in candidates:
        if not d:
            continue
        c = os.path.join(d, "claude")
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    if sys.platform == "darwin":
        mac_root = os.path.expanduser(
            "~/Library/Application Support/Claude/claude-code"
        )
        if os.path.isdir(mac_root):
            try:
                versions = sorted(os.listdir(mac_root), reverse=True)
            except OSError:
                versions = []
            for v in versions:
                cand = os.path.join(
                    mac_root, v, "claude.app/Contents/MacOS/claude"
                )
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    return cand
    return None


@app.get("/api/system/claude-auth-status")
async def claude_auth_status() -> dict[str, Any]:
    """Current authorisation status of the Claude Code CLI.

    Returns ``{"available": false}`` if the binary is missing,
    otherwise ``{"available": true, "loggedIn": bool, "authMethod": str}``.
    """
    bin_ = _resolve_claude_bin()
    if not bin_:
        return {"available": False, "reason": "claude binary not found"}
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_, "auth", "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        return {"available": True, "loggedIn": False, "reason": "timeout"}
    text = stdout_b.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"available": True, "loggedIn": False, "reason": "unparseable", "raw": text[:200]}
    return {
        "available": True,
        "loggedIn": bool(data.get("loggedIn", False)),
        "authMethod": data.get("authMethod"),
        "apiProvider": data.get("apiProvider"),
        "binary": bin_,
    }


@app.post("/api/system/claude-auth-login")
async def claude_auth_login() -> dict[str, Any]:
    """Runs ``claude auth login --claudeai`` in the background.

    The command opens the OAuth flow in a browser on its own. This endpoint
    does not wait for completion — it returns immediately. After a successful
    login the user sees the refreshed state by polling
    /api/system/claude-auth-status.
    """
    bin_ = _resolve_claude_bin()
    if not bin_:
        raise HTTPException(404, "claude binary not found")
    log_dir = Path(os.path.expanduser("~/Library/Logs/agent-kanban"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "claude-auth-login.log"
    f = open(log_path, "ab")
    proc = await asyncio.create_subprocess_exec(
        bin_, "auth", "login", "--claudeai",
        stdout=f, stderr=asyncio.subprocess.STDOUT,
    )
    return {
        "ok": True,
        "pid": proc.pid,
        "log": str(log_path),
        "note": "Open the browser if it does not launch automatically and finish OAuth.",
    }


import json  # noqa: E402  (used by claude_auth_status)


@app.post("/api/system/pick-folder")
async def pick_folder() -> dict[str, Any]:
    """Opens a native folder picker dialog (macOS / Linux+zenity / Windows).

    Returns ``{"path": "/abs/path"}`` or ``{"path": null, "cancelled": true}``
    when the user clicks Cancel. When no picker is available on the platform — 501.
    """
    if sys.platform == "darwin":
        cmd = ["osascript", "-e", _OSASCRIPT_PICK]
    elif sys.platform.startswith("linux"):
        if not shutil.which("zenity"):
            raise HTTPException(
                501,
                "Native picker requires 'zenity' on Linux. "
                "Enter the path manually or install zenity.",
            )
        cmd = ["zenity", "--file-selection", "--directory",
               "--title=Choose the project directory"]
    elif sys.platform == "win32":
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description='Choose the project directory'; "
            "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath } else { '' }"
        )
        cmd = ["powershell", "-NoProfile", "-Command", ps_script]
    else:
        raise HTTPException(501, f"folder picker is not supported on {sys.platform}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # 5 minutes — typical time for the user to pick a folder.
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        return {"path": None, "cancelled": True, "reason": "timeout"}
    path = stdout_b.decode("utf-8", errors="replace").strip()
    if not path:
        return {"path": None, "cancelled": True}
    # macOS POSIX path returns a trailing slash. Trim it.
    path = path.rstrip("/").rstrip("\\")
    return {"path": path, "cancelled": False}


_OSASCRIPT_PICK_FILE = """\
on run argv
  try
    if (count of argv) > 0 then
      set defaultLoc to POSIX file (item 1 of argv)
      set f to choose file with prompt "Choose the plan file" of type {"md","markdown","txt"} default location defaultLoc
    else
      set f to choose file with prompt "Choose the plan file" of type {"md","markdown","txt"}
    end if
    POSIX path of f
  on error
    return ""
  end try
end run
"""


@app.post("/api/system/pick-file")
async def pick_file(default_location: str = "") -> dict[str, Any]:
    """Opens a native file picker dialog. macOS only for now."""
    if sys.platform != "darwin":
        raise HTTPException(
            501, "file picker currently works only on macOS — enter the path manually"
        )
    args = ["osascript", "-e", _OSASCRIPT_PICK_FILE]
    if default_location:
        args.append(default_location)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        return {"path": None, "cancelled": True, "reason": "timeout"}
    path = stdout_b.decode("utf-8", errors="replace").strip()
    if not path:
        return {"path": None, "cancelled": True}
    return {"path": path, "cancelled": False}


# ---------------------------------------------------------------------------
# Streamable HTTP MCP transport (mounted at /mcp).
# ---------------------------------------------------------------------------
#
# Exposes the REST endpoints above as MCP tools over HTTP, so clients that
# don't speak stdio (Cursor, new Cline versions, MCP Inspector, Open WebUI)
# can attach without running a separate process. Parallel to the legacy
# stdio server in ``kanban_mcp/`` — Claude Code's ``.mcp.json`` continues
# to use stdio.
from fastapi_mcp import FastApiMCP  # noqa: E402

_mcp_http = FastApiMCP(
    app,
    name="agent-kanban",
    description=(
        "Local-first kanban for AI-agent workflows. Drag a task to Approved "
        "and your AI agent drives it through analyst → in_progress → testing."
    ),
)
_mcp_http.mount()
log.info("HTTP MCP mounted at /mcp")

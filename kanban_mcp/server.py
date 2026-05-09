"""agent-kanban — MCP server.

stdio transport. Registered in ``~/.claude.json`` (Claude Code) or
``.mcp.json`` (workspace scope) using the format:

    {
      "mcpServers": {
        "agent-kanban": {
          "type": "stdio",
          "command": "<repo>/.venv/bin/python",
          "args":    ["-m", "kanban_mcp"],
          "cwd":     "<repo>"
        }
      }
    }

The same setup works for Cline (Settings → MCP Servers → Add).

Tools (actor = "claude" by default, overridable via parameter):

* ``kanban_list``    — list tasks (filter by status / assignee)
* ``kanban_get``     — full card with history
* ``kanban_pull``    — atomically "claim a task" (approved → analyst, assignee=claude)
* ``kanban_move``    — move a task to a new status
* ``kanban_comment`` — comment in history
* ``kanban_create``  — new card
* ``kanban_link``    — add a link (memory / file / pr / url)
* ``kanban_columns`` — column descriptions (so the agent gets oriented)

On failure a human-readable message is returned; the MCP layer does not crash.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from kanban_store import Store, STATUSES, status_meta
from kanban_store.store import DEFAULT_PROJECT_ID


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_store: Store | None = None


def _get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def _err(msg: str) -> dict[str, Any]:
    return {"ok": False, "error": msg}


def _ok(payload: Any) -> dict[str, Any]:
    return {"ok": True, "data": payload}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = FastMCP("agent-kanban")


@mcp.tool()
def kanban_columns() -> dict[str, Any]:
    """Describe every kanban column and who moves cards in/out of it.

    Call this at the start of a session to understand the current status model.
    """
    return _ok({"columns": status_meta(), "statuses": STATUSES})


def _short_task(t: Any) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "status": t.status,
        "priority": t.priority,
        "size": t.size,
        "assignee": t.assignee,
        "external_blocker": t.external_blocker,
        "moved_at": t.moved_at,
        "blockers": t.blockers,
        "project_id": t.project_id,
    }


@mcp.tool()
def kanban_list(
    status: str | None = None,
    assignee: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """List tasks with optional filters.

    Args:
        status: one of backlog/approved/analyst/in_progress/testing/uat/done/blocked/cancelled,
                or None for all.
        assignee: claude / agent:<name> / user, or None for all.
        project_id: project slug (see kanban_projects); None = all projects.

    Returns:
        {"ok": true, "data": {"tasks": [{...short fields}], "count": N}}
    """
    try:
        tasks = _get_store().list_tasks(
            status=status, assignee=assignee, project_id=project_id,
        )
    except Exception as e:
        return _err(str(e))
    return _ok({
        "tasks": [_short_task(t) for t in tasks],
        "count": len(tasks),
    })


@mcp.tool()
def kanban_projects() -> dict[str, Any]:
    """List projects with task counts per column.

    Call this at the start of a session to learn which projects exist.

    Returns:
        {"ok": true, "data": {"projects": [
            {"id": "finops", "name": "FinOps", "color": "#F10D30",
             "path": "/abs/path", "task_counts": {"backlog": 5, ...},
             "total_tasks": 33, "archived": false}
        ]}}
    """
    try:
        projects = _get_store().list_projects(include_archived=False)
    except Exception as e:
        return _err(str(e))
    return _ok({"projects": [p.to_public() for p in projects]})


@mcp.tool()
def kanban_board(project_id: str) -> dict[str, Any]:
    """Compact overview of a project board: counts + first 5 tasks per column.

    Perfect for a quick "what's in progress?" answer without enumerating
    all 100+ tasks.

    Args:
        project_id: project slug (see kanban_projects).
    """
    try:
        proj = _get_store().get_project(project_id)
        if proj is None:
            return _err(f"project {project_id} not found")
        tasks = _get_store().list_tasks(project_id=project_id)
    except Exception as e:
        return _err(str(e))
    by_status: dict[str, list[dict[str, Any]]] = {s: [] for s in STATUSES}
    for t in tasks:
        by_status[t.status].append(_short_task(t))
    summary = {
        "project": {"id": proj.id, "name": proj.name, "path": proj.path},
        "total": len(tasks),
        "by_status": {
            s: {
                "count": len(by_status[s]),
                "first_5": by_status[s][:5],
            }
            for s in STATUSES if by_status[s]
        },
    }
    return _ok(summary)


@mcp.tool()
def kanban_search(query: str, project_id: str | None = None) -> dict[str, Any]:
    """Search tasks by substring in title/description (case-insensitive).

    Args:
        query: search term; rejected if shorter than 2 characters.
        project_id: limit the search to one project; None = all.
    """
    if not query or len(query.strip()) < 2:
        return _err("query must be at least 2 characters")
    q = query.strip().lower()
    try:
        tasks = _get_store().list_tasks(project_id=project_id)
    except Exception as e:
        return _err(str(e))
    hits = [t for t in tasks if q in t.title.lower() or q in (t.description or "").lower()]
    return _ok({
        "tasks": [_short_task(t) for t in hits],
        "count": len(hits),
        "query": query,
    })


@mcp.tool()
def kanban_my_active(
    assignee: str = "claude",
    project_id: str | None = None,
) -> dict[str, Any]:
    """Active tasks for the given assignee — in analyst/in_progress/testing.

    Perfect as the first query of a session: "what am I working on right now?"

    Args:
        assignee: claude (default), agent:<name>, user, ...
        project_id: project slug; None = all projects.
    """
    try:
        tasks = _get_store().list_tasks(assignee=assignee, project_id=project_id)
    except Exception as e:
        return _err(str(e))
    active = [t for t in tasks if t.status in ("analyst", "in_progress", "testing")]
    return _ok({
        "tasks": [_short_task(t) for t in active],
        "count": len(active),
        "assignee": assignee,
    })


@mcp.tool()
def kanban_get(task_id: str) -> dict[str, Any]:
    """Full task card: description, acceptance, links, history."""
    try:
        t = _get_store().get_task(task_id)
    except Exception as e:
        return _err(str(e))
    if not t:
        return _err(f"task {task_id} not found")
    return _ok(t.to_public())


@mcp.tool()
def kanban_pull(task_id: str, assignee: str = "claude") -> dict[str, Any]:
    """Atomically "claim a task" from Approved → Analyst.

    Conditions: the task must be in status ``approved`` AND its assignee
    must be either None or equal to ``assignee``. If another agent has
    already taken it, you get an error — try a different task. project_id
    is taken from the task itself, so you do not need to provide it.
    """
    try:
        t = _get_store().pull_task(task_id, assignee=assignee)
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok(t.to_public())


@mcp.tool()
def kanban_move(
    task_id: str,
    to_status: str,
    comment: str | None = None,
    actor: str = "claude",
) -> dict[str, Any]:
    """Move a task to a new status.

    Args:
        task_id: T-XXX
        to_status: target status (see kanban_columns).
        comment: optional comment, recorded in history.
        actor: claude / agent:<name>; defaults to claude.
    """
    if to_status not in STATUSES:
        return _err(f"unknown status: {to_status}; valid: {STATUSES}")
    try:
        t = _get_store().move_task(task_id, to_status, actor=actor, comment=comment)
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok(t.to_public())


@mcp.tool()
def kanban_comment(
    task_id: str,
    text: str,
    actor: str = "claude",
) -> dict[str, Any]:
    """Add a comment to the task's history.

    Useful for recording the plan once the task lands in Analyst, or for
    capturing a test result.
    """
    try:
        _get_store().add_comment(task_id, text, actor=actor)
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok({"task_id": task_id, "comment_added": True})


@mcp.tool()
def kanban_create(
    title: str,
    description: str = "",
    acceptance: str = "",
    status: str = "backlog",
    priority: str = "normal",
    size: str = "M",
    external_blocker: str | None = None,
    actor: str = "claude",
    project_id: str | None = None,
) -> dict[str, Any]:
    """Create a new card. Defaults to Backlog; for immediate work pass status='in_progress'.

    Args:
        title: a short single-line title.
        description: markdown with the details.
        acceptance: acceptance criteria (what counts as "done").
        priority: high / normal / low.
        size: S (<30 min) / M (<2 h) / L (>2 h).
        project_id: project slug; None = default (see KANBAN_DEFAULT_PROJECT_ID
                    or KANBAN_PROJECT_ID env).
    """
    if status not in STATUSES:
        return _err(f"unknown status: {status}")
    pid = project_id or os.environ.get("KANBAN_PROJECT_ID") or DEFAULT_PROJECT_ID
    try:
        t = _get_store().create_task(
            title=title,
            description=description,
            acceptance=acceptance,
            status=status,
            priority=priority,
            size=size,
            external_blocker=external_blocker,
            actor=actor,
            project_id=pid,
        )
    except Exception as e:
        return _err(str(e))
    return _ok(t.to_public())


@mcp.tool()
def kanban_link(task_id: str, link_type: str, value: str) -> dict[str, Any]:
    """Attach a link (memory/file/pr/url) to a task.

    Args:
        link_type: memory | file | pr | url
        value: file name or URL.
    """
    if link_type not in {"memory", "file", "pr", "url"}:
        return _err(f"unknown link_type: {link_type}")
    try:
        _get_store().add_link(task_id, link_type, value)
    except Exception as e:
        return _err(str(e))
    return _ok({"task_id": task_id, "link": {"type": link_type, "value": value}})


@mcp.tool()
def kanban_blockers(task_id: str, blocker_ids: list[str]) -> dict[str, Any]:
    """Replace the list of internal blockers (dependencies)."""
    try:
        _get_store().set_blockers(task_id, blocker_ids)
    except Exception as e:
        return _err(str(e))
    return _ok({"task_id": task_id, "blockers": blocker_ids})


@mcp.tool()
def kanban_update(
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    acceptance: str | None = None,
    priority: str | None = None,
    size: str | None = None,
    external_blocker: str | None = None,
    actor: str = "claude",
) -> dict[str, Any]:
    """Update any card fields (except status/assignee — use kanban_move/kanban_pull for those)."""
    try:
        t = _get_store().update_fields(
            task_id,
            actor=actor,
            title=title,
            description=description,
            acceptance=acceptance,
            priority=priority,
            size=size,
            external_blocker=external_blocker,
        )
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok(t.to_public())


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

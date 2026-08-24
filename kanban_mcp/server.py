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

* ``kanban_list``    — list tasks (filter by status / assignee / project / parent / updated_since)
* ``kanban_get``     — full card with history
* ``kanban_context`` — agent context bundle (task + ancestors + comments + children + constraints)
* ``kanban_children``— direct children (summary or full cards)
* ``kanban_subtree`` — recursive descendant tree (epic → stories → tickets)
* ``kanban_pull``    — atomically "claim a task" (approved → analyst, assignee=claude)
* ``kanban_claim``   — atomically claim approved → in_progress (assignee=agent:<role>)
* ``kanban_move``    — move a task to a new status
* ``kanban_comment`` — comment in history
* ``kanban_chat`` / ``kanban_send_chat`` — read / append the persisted agent chat
* ``kanban_run`` / ``kanban_register_run`` — read / upsert the task_runs row
* ``kanban_stop`` / ``kanban_steer`` — stop or steer the task's live agent driver
* ``kanban_create``  — new card (supports parent_id / kind hierarchy)
* ``kanban_link``    — add a link (memory / file / pr / url)
* ``kanban_columns`` — column descriptions (so the agent gets oriented)
* ``kanban_projects`` / ``kanban_board`` / ``kanban_search`` / ``kanban_my_active`` / ``kanban_update`` / ``kanban_blockers``

On failure a human-readable message is returned; the MCP layer does not crash.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from kanban_store import STATUSES, Store, status_meta
from kanban_store.store import DEFAULT_PROJECT_ID
from kanban_ui.agent_control import ControlUnavailable, control_request


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
    """Summary card — same shape as the REST /api/tasks payload."""
    return {
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


@mcp.tool()
def kanban_list(
    status: str | None = None,
    assignee: str | None = None,
    project_id: str | None = None,
    parent_id: str | None = None,
    updated_since: str | None = None,
) -> dict[str, Any]:
    """List tasks with optional filters (parity with GET /api/tasks).

    Args:
        status: one of backlog/approved/analyst/in_progress/testing/uat/done/blocked/cancelled,
                or None for all.
        assignee: claude / agent:<name> / user, or None for all.
        project_id: project slug (see kanban_projects); None = all projects.
        parent_id: only direct children of this task (hierarchy: epic → story → task).
        updated_since: ISO8601 — only tasks with a history entry at or after
                this timestamp (create/move/comment/assign/update).

    Returns:
        {"ok": true, "data": {"tasks": [{...summary fields}], "count": N}}
    """
    try:
        tasks = _get_store().list_tasks(
            status=status,
            assignee=assignee,
            project_id=project_id,
            parent_id=parent_id,
            updated_since=updated_since,
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
    parent_id: str | None = None,
    kind: str = "task",
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
        parent_id: parent task id — hierarchy epic → story → task; the child
                    kind is derived from the parent (a story child is a task).
        kind: task | story | epic — top-level kinds only; ignored when a
                    parent_id forces the matching child kind.
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
            parent_id=parent_id,
            kind=kind,
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
# Agent context protocol + live-run control (parity with the REST API)
# ---------------------------------------------------------------------------


def _constraints() -> list[str]:
    """Shared agent constraints from examples/agents.json (same file the
    task-driver reads for role config). Missing/unreadable -> [].
    """
    try:
        data = json.loads(
            (Path(__file__).resolve().parent.parent / "examples" / "agents.json")
            .read_text(encoding="utf-8")
        )
        return list(data.get("constraints") or [])
    except (OSError, ValueError):
        return []


@mcp.tool()
def kanban_context(task_id: str) -> dict[str, Any]:
    """Agent context bundle for a task — parity with GET /api/tasks/{id}/context.

    One call gives a dispatched agent everything: task fields, the ancestor
    chain (epic description, story acceptance), the 20 most recent comments,
    a children summary, and the shared agent constraints from agents.json.
    """
    try:
        t = _get_store().get_task(task_id)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))
    if not t:
        return _err(f"task {task_id} not found")
    comments = [
        {"id": h.id, "ts": h.ts, "actor": h.actor, "text": h.comment}
        for h in t.history
        if h.action == "comment"
    ]
    return _ok({
        "task_id": t.id,
        "project_id": t.project_id,
        "task": {
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "priority": t.priority,
            "size": t.size,
            "assignee": t.assignee,
            "kind": t.kind,
            "parent_id": t.parent_id,
            "description": t.description,
            "acceptance": t.acceptance,
            "external_blocker": t.external_blocker,
            "created_at": t.created_at,
            "moved_at": t.moved_at,
        },
        "ancestors": t.ancestors,
        "comments": comments[-20:],
        "children": [
            {"id": c.id, "title": c.title, "status": c.status, "size": c.size}
            for c in _get_store().list_tasks(parent_id=t.id)
        ],
        "constraints": _constraints(),
    })


@mcp.tool()
def kanban_children(task_id: str, include: str = "summary") -> dict[str, Any]:
    """Direct children of a task — parity with GET /api/tasks/{id}/children.

    Args:
        task_id: parent task id.
        include: "summary" (default, same card shape as /api/tasks) or
                 "full" (complete child cards: description, acceptance,
                 parent chain, comments) — a scoping agent sees every
                 planned child without N+1 fetches.

    Returns:
        {"ok": true, "data": {"task_id": ..., "count": N, "children": [...]}}
    """
    if include not in ("summary", "full"):
        return _err("include must be 'summary' or 'full'")
    store = _get_store()
    try:
        if not store.get_task(task_id):
            return _err(f"task {task_id} not found")
        full = include == "full"
        children = store.list_tasks(
            parent_id=task_id, eager_history=full, eager_tree=full
        )
    except Exception as e:  # noqa: BLE001
        return _err(str(e))
    return _ok({
        "task_id": task_id,
        "count": len(children),
        "children": [c.to_public() if full else _short_task(c) for c in children],
    })


@mcp.tool()
def kanban_subtree(task_id: str) -> dict[str, Any]:
    """Recursive descendant tree — parity with GET /api/tasks/{id}/subtree.

    Epic → stories → tickets with full fields (description, acceptance,
    kind, status, size, assignee, parent_id, blockers) plus nested
    ``children`` per node in one call, no N+1. Sibling order follows
    (status, column_order, id). Use this first when scoping an epic.
    """
    store = _get_store()
    try:
        if not store.get_task(task_id):
            return _err(f"task {task_id} not found")
        descendants = store.get_descendants(task_id)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))
    by_parent: dict[str | None, list[Any]] = {}
    for d in descendants:
        by_parent.setdefault(d.parent_id, []).append(d)

    def _node(parent_key: str | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for d in by_parent.get(parent_key, []):
            node = {
                "id": d.id,
                "title": d.title,
                "status": d.status,
                "priority": d.priority,
                "size": d.size,
                "assignee": d.assignee,
                "external_blocker": d.external_blocker,
                "blockers": d.blockers,
                "parent_id": d.parent_id,
                "kind": d.kind,
                "moved_at": d.moved_at,
                "created_at": d.created_at,
                "project_id": d.project_id,
                "description": d.description,
                "acceptance": d.acceptance,
                "children": _node(d.id),
            }
            out.append(node)
        return out

    return _ok({"task_id": task_id, "tree": _node(task_id)})


@mcp.tool()
def kanban_claim(task_id: str, assignee: str) -> dict[str, Any]:
    """Atomically claim a task — parity with POST /api/tasks/{id}/claim.

    Sets assignee=agent:<role> and moves approved → in_progress in one
    history-recorded transaction. Idempotent for a re-claim by the same
    assignee; errors when the task is owned by someone else or not in
    approved/in_progress. Use this (not kanban_pull) for the driver claim.
    """
    if not assignee.strip():
        return _err("assignee required")
    try:
        t = _get_store().claim_task(task_id, assignee, actor="claude")
    except KeyError:
        return _err(f"task {task_id} not found")
    except RuntimeError as e:
        return _err(str(e))
    return _ok(t.to_public())


@mcp.tool()
def kanban_chat(task_id: str) -> dict[str, Any]:
    """Persisted agent chat for a task — parity with GET /api/tasks/{id}/chat.

    task_chat messages (role/content/ts/seq), ascending; survives the run.
    """
    store = _get_store()
    try:
        if not store.get_task(task_id):
            return _err(f"task {task_id} not found")
        messages = store.get_chat(task_id)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))
    return _ok({"task_id": task_id, "messages": messages})


@mcp.tool()
def kanban_send_chat(task_id: str, role: str, content: str) -> dict[str, Any]:
    """Append a message to a task's persisted chat — parity with POST /api/tasks/{id}/chat.

    Args:
        task_id: target task.
        role: author, e.g. agent:fe / user.
        content: message text.
    """
    try:
        msg = _get_store().add_chat_message(task_id, role, content)
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:  # noqa: BLE001
        return _err(str(e))
    return _ok(msg)


@mcp.tool()
def kanban_run(task_id: str) -> dict[str, Any]:
    """The task_runs row for a task — parity with GET /api/tasks/{id}/runs.

    Live agent run: pid, role, status, control_port, tokens_used, model,
    started_at/ended_at. None when the task has no registered run.
    """
    store = _get_store()
    try:
        if not store.get_task(task_id):
            return _err(f"task {task_id} not found")
        run = store.get_run(task_id)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))
    return _ok({"task_id": task_id, "run": run})


@mcp.tool()
def kanban_register_run(
    task_id: str,
    pid: int | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    model: str | None = None,
    role: str | None = None,
    status: str | None = None,
    tokens_used: int | None = None,
    control_port: int | None = None,
) -> dict[str, Any]:
    """Upsert the task_runs row — parity with POST /api/tasks/{id}/runs.

    Only the provided fields are written (the driver registers at start:
    pid/started_at/model/role/control_port/status, and on exit:
    ended_at/status/tokens_used). At least one field is required.
    """
    try:
        run = _get_store().register_run(
            task_id,
            pid=pid,
            started_at=started_at,
            ended_at=ended_at,
            model=model,
            role=role,
            status=status,
            tokens_used=tokens_used,
            control_port=control_port,
        )
    except KeyError:
        return _err(f"task {task_id} not found")
    except ValueError as e:
        return _err(str(e))
    return _ok({"task_id": task_id, "run": run})


def _live_run(task_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """task_runs row for a live agent run, or (None, error message)."""
    store = _get_store()
    try:
        if not store.get_task(task_id):
            return None, f"task {task_id} not found"
        run = store.get_run(task_id)
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    if not run or run.get("status") != "running" or not run.get("control_port"):
        return None, f"task {task_id} has no live agent run"
    return run, None


@mcp.tool()
def kanban_stop(
    task_id: str, reason: str, to_status: str = "blocked"
) -> dict[str, Any]:
    """Stop the task's live agent — parity with POST /api/tasks/{id}/agent/stop.

    Relays to the driver's control socket; the driver SIGINTs the omp
    session, posts ``reason`` as a comment and moves the task: "blocked"
    (human intervention, default) or "approved" (routine auto-retry, D2).
    409-style error when the task has no live run.
    """
    if to_status not in ("blocked", "approved"):
        return _err("to_status must be 'blocked' or 'approved'")
    run, err = _live_run(task_id)
    if err:
        return _err(err)
    try:
        resp = control_request(
            run["control_port"],
            {"cmd": "stop", "reason": reason, "to_status": to_status},
        )
    except ControlUnavailable as e:
        return _err(str(e))
    return _ok({"cmd": "stop", "driver": resp})


@mcp.tool()
def kanban_steer(task_id: str, text: str) -> dict[str, Any]:
    """Inject a message into the task's live agent session — parity with
    POST /api/tasks/{id}/agent/steer.

    Relays to the driver's control socket; the message is sent to the omp
    session as if the user typed it. 409-style error when the task has no
    live run.
    """
    if not text.strip():
        return _err("text is required")
    run, err = _live_run(task_id)
    if err:
        return _err(err)
    try:
        resp = control_request(
            run["control_port"],
            {"cmd": "steer", "text": text, "comment_id": None},
        )
    except ControlUnavailable as e:
        return _err(str(e))
    return _ok({"cmd": "steer", "driver": resp})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

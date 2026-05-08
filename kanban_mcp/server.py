"""agent-kanban — MCP server.

stdio transport. Регистрируется в ``~/.claude.json`` (Claude Code) или
``.mcp.json`` (workspace-scope), формат:

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

То же подходит для Cline (Settings → MCP Servers → Add).

Tools (actor = "claude" по умолчанию, переопределяется параметром):

* ``kanban_list``   — список задач (фильтр по status / assignee)
* ``kanban_get``    — полная карточка с history
* ``kanban_pull``   — атомарно «беру задачу» (approved → analyst, assignee=claude)
* ``kanban_move``   — перевод задачи в новый статус
* ``kanban_comment``— коммент в history
* ``kanban_create`` — новая карточка
* ``kanban_link``   — добавить ссылку (memory / file / pr / url)
* ``kanban_columns``— описание колонок (для ориентации агента)

При ошибке возвращается человекочитаемое сообщение, MCP-уровень не падает.
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
    """Описание всех колонок канбана и кто двигает в/из них.

    Используй в начале сессии чтобы понять текущую модель статусов.
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
    """Список задач с опциональным фильтром.

    Args:
        status: один из backlog/approved/analyst/in_progress/testing/uat/done/blocked/cancelled,
                или None для всех.
        assignee: claude / agent:<name> / user, или None для всех.
        project_id: slug проекта (см. kanban_projects); None = все проекты.

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
    """Список проектов с подсчётом задач по колонкам.

    Используй в начале сессии, чтобы понять какие проекты есть.

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
    """Компактный обзор доски проекта: счётчики + первые 5 задач каждой колонки.

    Идеально для быстрого ответа «что в работе?» без перечисления всех 100+ задач.

    Args:
        project_id: slug проекта (см. kanban_projects).
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
    """Поиск задач по подстроке в title/description (case-insensitive).

    Args:
        query: что искать; <2 символов отклоняется.
        project_id: ограничить поиск проектом; None = все.
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
    """Активные задачи указанного исполнителя — в analyst/in_progress/testing.

    Идеально как первый запрос в сессии: «что у меня сейчас в работе?»

    Args:
        assignee: claude (default), agent:<name>, user, ...
        project_id: slug проекта; None = все проекты.
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
    """Полная карточка задачи: описание, acceptance, links, history."""
    try:
        t = _get_store().get_task(task_id)
    except Exception as e:
        return _err(str(e))
    if not t:
        return _err(f"task {task_id} not found")
    return _ok(t.to_public())


@mcp.tool()
def kanban_pull(task_id: str, assignee: str = "claude") -> dict[str, Any]:
    """Атомарно «беру задачу» из Согласовано → Аналитика.

    Условия: задача в статусе ``approved`` И assignee либо None, либо равен ``assignee``.
    Если уже взята другим агентом — вернёт ошибку, попробуй другую задачу.
    project_id берётся из самой задачи — указывать не нужно.
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
    """Перевести задачу в новый статус.

    Args:
        task_id: T-XXX
        to_status: целевой статус (см. kanban_columns).
        comment: опциональный комментарий, попадает в history.
        actor: claude / agent:<name>; по умолчанию claude.
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
    """Добавить коммент в history задачи.

    Полезно для записи плана при попадании в Аналитику, или результата теста.
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
    """Новая карточка. По умолчанию в Бэклог; чтоб сразу в работу — status='in_progress'.

    Args:
        title: коротко в одну строку.
        description: markdown с подробностями.
        acceptance: критерии приёмки (что считается «сделано»).
        priority: high / normal / low.
        size: S (<30 мин) / M (<2 ч) / L (>2 ч).
        project_id: slug проекта; None = дефолтный (см. KANBAN_DEFAULT_PROJECT_ID
                    или KANBAN_PROJECT_ID env).
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
    """Привязать ссылку (memory/file/pr/url) к задаче.

    Args:
        link_type: memory | file | pr | url
        value: имя файла или url.
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
    """Перезаписать список внутренних блокеров (зависимостей)."""
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
    """Обновить любые поля карточки (кроме статуса/assignee — для них kanban_move/kanban_pull)."""
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

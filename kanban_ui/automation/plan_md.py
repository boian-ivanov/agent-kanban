"""PLAN.md ↔ kanban synchronisation.

File format:

    # ProjectName — Plan

    > The kanban reads this file. Every `- [ ] ...` line under a section
    > maps to a card in that column. Do not change the heading format —
    > the engine matches them strictly.

    ## Backlog
    - [ ] TTL for internal.* tables
    - [ ] Rotate the web password

    ## In progress
    - [ ] Coder anti-loop counter

    ## Done
    - [x] Inbox watcher

This module provides:
- ``parse_plan_md(text)`` — sections → ``status -> [titles]``.
- ``init_plan_md(path, project)`` — create an empty template.
- ``update_claude_md(path, project)`` — append the kanban-board block.
- ``import_plan_md(store, project_id, file_path)`` — create tasks in the
  kanban from the file's contents; idempotent by (project_id, title).

Two-way sync (UI edits → back to the file) is a separate feature.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from kanban_store import Store

log = logging.getLogger("kanban.plan_md")

# Heading-to-status mapping (case-insensitive, punctuation stripped).
HEADING_TO_STATUS: dict[str, str] = {
    "backlog": "backlog", "бэклог": "backlog",
    "approved": "approved", "согласовано": "approved",
    "analyst": "analyst", "analytics": "analyst", "аналитика": "analyst",
    "in progress": "in_progress", "wip": "in_progress",
    "in_progress": "in_progress", "в работе": "in_progress",
    "testing": "testing", "qa": "testing", "тестирование": "testing",
    "uat": "uat", "acceptance": "uat", "приёмка": "uat", "приемка": "uat",
    "done": "done", "closed": "done", "закрыто": "done",
    "blocked": "blocked", "заблокировано": "blocked",
    "cancelled": "cancelled", "canceled": "cancelled", "отменено": "cancelled",
}

STATUS_LABELS_RU = {
    "backlog":     "Backlog",
    "approved":    "Approved",
    "analyst":     "Analyst",
    "in_progress": "In progress",
    "testing":     "Testing",
    "uat":         "UAT",
    "done":        "Done",
    "blocked":     "Blocked",
    "cancelled":   "Cancelled",
}

_HEADING_RE = re.compile(r"^##+\s+(.+?)\s*$")
_TASK_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s+(.+?)\s*$")


@dataclass(frozen=True)
class ParsedTask:
    """A single `- [ ]` line from a plan file.

    ``status`` — where the task ends up: either a canonical status
    (``backlog``, ``done`` etc.) or ``backlog`` if the section does not
    map to a status. ``section_label`` is the raw section heading (with
    emoji or numbers preserved) for loose sections; ``None`` for
    canonical sections (when the heading was found in ``HEADING_TO_STATUS``).
    """
    title: str
    done: bool
    status: str
    section_label: str | None


def _norm_heading(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.strip().lower()).strip()


def parse_plan_md(text: str) -> list[ParsedTask]:
    """Parses PLAN.md into a list of ``ParsedTask`` (order preserved).

    Behaviour:

    * Headings that map via ``HEADING_TO_STATUS`` (``## Backlog``,
      ``## In progress``, ``## Done`` etc.) — tasks are placed into the
      corresponding column with ``section_label=None``.
    * Any other heading (``## 🔴 Tier 0``, ``## v2 stages``,
      ``## Status snapshot``) is a loose section; tasks under it land in
      ``backlog`` and the section name (as written) goes into
      ``section_label`` for later display in the description.
    * Tasks above the first ``##`` heading are ignored.
    """
    result: list[ParsedTask] = []
    current_status: str | None = None
    current_label: str | None = None
    for raw in text.splitlines():
        m_h = _HEADING_RE.match(raw)
        if m_h:
            raw_heading = m_h.group(1).strip()
            mapped = HEADING_TO_STATUS.get(_norm_heading(raw_heading))
            if mapped is not None:
                current_status = mapped
                current_label = None
            else:
                current_status = "backlog"
                current_label = raw_heading
            continue
        if current_status is None:
            continue
        m_t = _TASK_RE.match(raw)
        if m_t:
            done = m_t.group(1).lower() == "x"
            title = m_t.group(2).strip()
            if title:
                result.append(ParsedTask(
                    title=title,
                    done=done,
                    status=current_status,
                    section_label=current_label,
                ))
    return result


def render_plan_template(project_name: str, project_id: str) -> str:
    """Template for a new PLAN.md."""
    return (
        f"# {project_name} — Plan\n\n"
        f"> This file is synced with the kanban board: "
        f"http://localhost:7777/p/{project_id}\n"
        f"> Every `- [ ] ...` line under a column heading = a card in "
        f"that column.\n"
        f"> Allowed column headings: Backlog, Approved, Analyst, "
        f"In progress, Testing, UAT, Done, Blocked, Cancelled (Russian also OK).\n\n"
        f"## Backlog\n\n"
        f"- [ ] (new tasks land here)\n\n"
        f"## In progress\n\n"
        f"## Done\n"
    )


CLAUDE_MD_BLOCK_MARKER = "<!-- KANBAN-BOARD-BLOCK -->"

CLAUDE_MD_TEMPLATE = """\
{marker}
## Kanban board ({project_name})

Write every new task for this project into [PLAN.md]({plan_path}) as a
`- [ ] {{title}}` line under `## Backlog`. When you start a task, move
it under `## In progress`. Closed → `## Done`. Blocked → `## Blocked`.

The kanban board — http://localhost:7777/p/{project_id} — is synced with
this file. UI changes update PLAN.md (two-way sync will be added in a
future release; for now only file → kanban).

Supported columns (PLAN.md headings): Backlog, Approved, Analyst,
In progress, Testing, UAT, Done, Blocked, Cancelled.
{marker_end}
"""


def update_claude_md(claude_md_path: Path, project_id: str, project_name: str,
                     plan_relative: str = "PLAN.md") -> None:
    """Adds/updates the "Kanban board" block in CLAUDE.md.

    If the file does not exist, it is created. If it does, the marker is
    located and the block is replaced.
    """
    block = CLAUDE_MD_TEMPLATE.format(
        marker=CLAUDE_MD_BLOCK_MARKER,
        marker_end=CLAUDE_MD_BLOCK_MARKER + " end",
        project_name=project_name,
        project_id=project_id,
        plan_path=plan_relative,
    )
    if not claude_md_path.exists():
        claude_md_path.write_text(block, encoding="utf-8")
        return
    existing = claude_md_path.read_text(encoding="utf-8")
    if CLAUDE_MD_BLOCK_MARKER in existing:
        # Replace the block between marker and marker_end
        pattern = re.compile(
            re.escape(CLAUDE_MD_BLOCK_MARKER) + r".*?"
            + re.escape(CLAUDE_MD_BLOCK_MARKER + " end") + r"\n?",
            re.DOTALL,
        )
        new = pattern.sub(block, existing)
        claude_md_path.write_text(new, encoding="utf-8")
    else:
        sep = "\n" if not existing.endswith("\n") else ""
        claude_md_path.write_text(existing + sep + "\n" + block, encoding="utf-8")


def init_plan_md(path: Path, project_id: str, project_name: str,
                 *, overwrite: bool = False) -> Path:
    """Creates PLAN.md in the given directory. Returns the path to the created file."""
    plan_path = path / "PLAN.md"
    if plan_path.exists() and not overwrite:
        return plan_path
    path.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        render_plan_template(project_name, project_id), encoding="utf-8"
    )
    return plan_path


def import_plan_md(store: Store, project_id: str, plan_file: Path) -> dict[str, int]:
    """Parses PLAN.md and creates tasks in the project.

    Idempotent: a task with the same title in this project is not created
    twice. If a task came from a loose section (``section_label`` is not
    None), its description gets a ``_From section:_ **<label>**`` prefix
    that is visible on the card as context.
    Returns ``{"created": N, "skipped": K}``.
    """
    text = plan_file.read_text(encoding="utf-8")
    parsed = parse_plan_md(text)
    existing_titles = {t.title for t in store.list_tasks(project_id=project_id)}
    created = 0
    skipped = 0
    for task in parsed:
        if task.title in existing_titles:
            skipped += 1
            continue
        # If a [x] line and the status is not cancelled, mark it as done.
        real_status = (
            "done" if task.done and task.status != "cancelled" else task.status
        )
        description = (
            f"_From section:_ **{task.section_label}**\n\n"
            if task.section_label
            else ""
        )
        store.create_task(
            title=task.title,
            description=description,
            status=real_status,
            project_id=project_id,
            actor="plan-import",
        )
        existing_titles.add(task.title)
        created += 1
    log.info("plan_md import for %s: created=%d skipped=%d", project_id, created, skipped)
    return {"created": created, "skipped": skipped}

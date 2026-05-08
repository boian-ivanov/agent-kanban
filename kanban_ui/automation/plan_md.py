"""PLAN.md ↔ kanban синхронизация.

Формат файла:

    # ProjectName — Plan

    > Канбан читает этот файл. Каждая строка `- [ ] ...` под секцией
    > соответствует карточке в этой колонке. Не редактируйте формат
    > заголовков — engine ищет их жёстко.

    ## Backlog
    - [ ] TTL для internal.* таблиц
    - [ ] Сменить пароль web

    ## In progress
    - [ ] Coder anti-loop counter

    ## Done
    - [x] Inbox watcher

Этот модуль умеет:
- ``parse_plan_md(text)`` — секции → ``status -> [titles]``.
- ``init_plan_md(path, project)`` — создать пустой шаблон.
- ``update_claude_md(path, project)`` — append блок про канбан-доску.
- ``import_plan_md(store, project_id, file_path)`` — создаёт задачи в
  канбане по содержимому файла; идемпотентно по (project_id, title).

Двухсторонний sync (изменения в UI → обратно в файл) — отдельная фича.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from kanban_store import Store

log = logging.getLogger("kanban.plan_md")

# Маппинг заголовков (case-insensitive, без знаков пунктуации) на статусы.
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
    """Одна `- [ ]` строка из plan-файла.

    ``status`` — куда задача попадёт: либо canonical статус (``backlog``,
    ``done`` и т.д.), либо ``backlog`` если секция не маппится на статус.
    ``section_label`` — raw heading секции (с эмодзи и числами как есть)
    для loose-секций; ``None`` для canonical (когда heading нашёлся в
    ``HEADING_TO_STATUS``).
    """
    title: str
    done: bool
    status: str
    section_label: str | None


def _norm_heading(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.strip().lower()).strip()


def parse_plan_md(text: str) -> list[ParsedTask]:
    """Парсит PLAN.md в список ``ParsedTask`` (порядок сохраняется).

    Поведение:

    * Заголовки, которые маппятся через ``HEADING_TO_STATUS``
      (``## Backlog``, ``## In progress``, ``## Done`` etc.) — задачи
      попадают в соответствующую колонку, ``section_label=None``.
    * Любые другие заголовки (``## 🔴 Tier 0``, ``## v2 этапы``,
      ``## Status snapshot``) — это loose-секции; задачи под ними
      попадают в ``backlog``, имя секции (как написано) — в
      ``section_label`` для последующего отображения в description.
    * Задачи до первого ``##`` игнорируются.
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
    """Шаблон для нового PLAN.md."""
    return (
        f"# {project_name} — Plan\n\n"
        f"> Этот файл синхронизируется с канбан-доской: "
        f"http://localhost:7777/p/{project_id}\n"
        f"> Каждая строка `- [ ] ...` под заголовком колонки = карточка в "
        f"этой колонке.\n"
        f"> Допустимые заголовки колонок: Backlog, Approved, Analyst, "
        f"In progress, Testing, UAT, Done, Blocked, Cancelled (русский тоже OK).\n\n"
        f"## Backlog\n\n"
        f"- [ ] (новые задачи попадают сюда)\n\n"
        f"## In progress\n\n"
        f"## Done\n"
    )


CLAUDE_MD_BLOCK_MARKER = "<!-- KANBAN-BOARD-BLOCK -->"

CLAUDE_MD_TEMPLATE = """\
{marker}
## Канбан-доска ({project_name})

Все новые задачи этого проекта пиши в [PLAN.md]({plan_path}) — строкой
`- [ ] {{title}}` под `## Backlog`. Когда берёшь задачу в работу — перенеси
её под `## In progress`. Закрытые → `## Done`. Заблокированные → `## Blocked`.

Канбан-доска — http://localhost:7777/p/{project_id} — синхронизируется
с этим файлом. Изменения в UI обновляют PLAN.md (двусторонний sync будет
добавлен в следующем релизе; пока что file → канбан).

Поддерживаемые колонки (заголовки в PLAN.md): Backlog, Approved, Analyst,
In progress, Testing, UAT, Done, Blocked, Cancelled.
{marker_end}
"""


def update_claude_md(claude_md_path: Path, project_id: str, project_name: str,
                     plan_relative: str = "PLAN.md") -> None:
    """Добавляет/обновляет блок «Канбан-доска» в CLAUDE.md.

    Если файла нет — создаёт. Если есть — ищет marker и заменяет блок.
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
        # Заменяем блок между marker и marker_end
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
    """Создаёт PLAN.md в указанной директории. Возвращает путь к созданному файлу."""
    plan_path = path / "PLAN.md"
    if plan_path.exists() and not overwrite:
        return plan_path
    path.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        render_plan_template(project_name, project_id), encoding="utf-8"
    )
    return plan_path


def import_plan_md(store: Store, project_id: str, plan_file: Path) -> dict[str, int]:
    """Парсит PLAN.md и создаёт задачи в проекте.

    Идемпотентно: задача с тем же title в этом проекте не создаётся повторно.
    Если задача пришла из loose-секции (``section_label`` не None),
    в её description добавляется префикс ``_From section:_ **<label>**`` —
    видимый в карточке как контекст.
    Возвращает ``{"created": N, "skipped": K}``.
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
        # Если строка [x] и статус не cancelled — ставим done.
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

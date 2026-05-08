"""Inbox watcher: следит за папкой ``KANBAN_INBOX_DIR``, превращает
``.md`` файлы в задачи. Работает через polling (без watchdog dep).

Формат файла — markdown с YAML frontmatter::

    ---
    title: TTL для internal.* таблиц
    project_id: default
    status: backlog
    priority: high
    size: M
    external_blocker: DBA — нужно согласование
    links:
      - type: file
        value: etl/load_internal.sh
    ---
    Описание задачи. Тело markdown идёт в `description`.

    ## Acceptance criteria
    - [ ] Все таблицы имеют TTL
    - [ ] Старые партиции удалены

После успешного импорта файл переезжает в ``inbox/processed/YYYY-MM-DD/``.
При ошибке — в ``inbox/failed/`` с ``.error`` файлом-логом.

Все поля frontmatter опциональны. Если ``title`` отсутствует — берётся
имя файла (без ``.md``, ``_`` → пробелы).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from kanban_store import Store

log = logging.getLogger("kanban.automation.inbox")

POLL_INTERVAL = float(os.environ.get("KANBAN_INBOX_INTERVAL", "5"))

# Заголовок acceptance — всё что под этим хедером уходит в task.acceptance.
_ACCEPTANCE_RE = re.compile(
    r"^##+\s*(?:acceptance(?:\s+criteria)?|критерии\s+приёмки)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Простая статистика для status endpoint.
_status: dict[str, Any] = {
    "running": False,
    "watch_dir": None,
    "interval_sec": POLL_INTERVAL,
    "last_scan_at": None,
    "imported_total": 0,
    "failed_total": 0,
    "last_imports": [],   # последние 10 импортов: {ts, file, task_id}
    "last_errors":  [],   # последние 10 ошибок:   {ts, file, error}
}


def inbox_status() -> dict[str, Any]:
    return dict(_status)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_dir() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _slug_to_title(filename: str) -> str:
    name = filename
    if name.endswith(".md"):
        name = name[:-3]
    return name.replace("_", " ").replace("-", " ").strip()


def _parse_markdown(content: str) -> tuple[dict[str, Any], str]:
    """Возвращает (frontmatter_dict, body)."""
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content
    fm_text = content[3:end].strip()
    body = content[end + 4:].lstrip("\n")
    try:
        fm = yaml.safe_load(fm_text) or {}
        if not isinstance(fm, dict):
            return {}, content
        return fm, body
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML frontmatter: {e}") from e


def _split_acceptance(body: str) -> tuple[str, str]:
    """Делит тело на (description, acceptance) по заголовку
    ``## Acceptance criteria`` (или русскому аналогу)."""
    m = _ACCEPTANCE_RE.search(body)
    if not m:
        return body.strip(), ""
    description = body[:m.start()].rstrip()
    # acceptance — всё после этого заголовка до следующего # ## или конца файла
    rest = body[m.end():]
    next_header = re.search(r"^##+\s+\w", rest, re.MULTILINE)
    acceptance = (rest[:next_header.start()] if next_header else rest).strip()
    return description, acceptance


def _import_file(store: Store, path: Path) -> str:
    """Создаёт задачу из файла. Возвращает task_id."""
    content = path.read_text(encoding="utf-8")
    fm, body = _parse_markdown(content)
    title = fm.get("title") or _slug_to_title(path.name)
    description, acceptance = _split_acceptance(body)

    project_id = fm.get("project_id") or os.environ.get(
        "KANBAN_DEFAULT_PROJECT_ID", "default"
    )
    if store.get_project(project_id) is None:
        raise ValueError(f"project '{project_id}' not found")

    links = fm.get("links")
    if links is not None:
        if not isinstance(links, list):
            raise ValueError("'links' must be a list of {type, value}")
        for ln in links:
            if not isinstance(ln, dict) or "type" not in ln or "value" not in ln:
                raise ValueError(
                    "each link must have 'type' (memory/file/pr/url) and 'value'"
                )

    t = store.create_task(
        title=str(title).strip(),
        description=description,
        acceptance=acceptance,
        status=fm.get("status", "backlog"),
        priority=fm.get("priority", "normal"),
        size=fm.get("size", "M"),
        external_blocker=fm.get("external_blocker"),
        actor="inbox",
        links=links,
        project_id=project_id,
    )
    return t.id


def _move_to_processed(path: Path, root: Path) -> Path:
    dest_dir = root / "processed" / _today_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    # Если дубль — добавляем счётчик
    n = 1
    while dest.exists():
        n += 1
        dest = dest_dir / f"{path.stem}__{n}{path.suffix}"
    shutil.move(str(path), str(dest))
    return dest


def _move_to_failed(path: Path, root: Path, error: str) -> Path:
    fail_dir = root / "failed"
    fail_dir.mkdir(parents=True, exist_ok=True)
    dest = fail_dir / path.name
    n = 1
    while dest.exists():
        n += 1
        dest = fail_dir / f"{path.stem}__{n}{path.suffix}"
    shutil.move(str(path), str(dest))
    err_path = dest.with_suffix(dest.suffix + ".error")
    err_path.write_text(f"{_now_iso()}\n{error}\n", encoding="utf-8")
    return dest


def _scan_once(store: Store, watch_dir: Path) -> None:
    if not watch_dir.exists():
        return
    for entry in sorted(watch_dir.iterdir()):
        if entry.is_dir():
            continue
        if entry.suffix.lower() != ".md":
            continue
        if entry.name.startswith("."):
            continue
        try:
            task_id = _import_file(store, entry)
            _move_to_processed(entry, watch_dir)
            _status["imported_total"] += 1
            _status["last_imports"].insert(
                0, {"ts": _now_iso(), "file": entry.name, "task_id": task_id}
            )
            _status["last_imports"] = _status["last_imports"][:10]
            log.info("inbox: imported %s -> %s", entry.name, task_id)
        except Exception as e:
            _move_to_failed(entry, watch_dir, str(e))
            _status["failed_total"] += 1
            _status["last_errors"].insert(
                0, {"ts": _now_iso(), "file": entry.name, "error": str(e)}
            )
            _status["last_errors"] = _status["last_errors"][:10]
            log.exception("inbox: failed to import %s", entry.name)


class InboxWatcher:
    """Async-loop, который раз в POLL_INTERVAL сканирует inbox.

    Использование (см. lifespan):
        watcher = InboxWatcher(store, watch_dir)
        task = asyncio.create_task(watcher.run())
        ...
        watcher.stop(); await task
    """

    def __init__(self, store: Store, watch_dir: Path):
        self.store = store
        self.watch_dir = watch_dir
        self._stop = asyncio.Event()

    async def run(self) -> None:
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        _status["running"] = True
        _status["watch_dir"] = str(self.watch_dir)
        log.info("inbox watcher started: %s (interval=%ss)", self.watch_dir, POLL_INTERVAL)
        try:
            while not self._stop.is_set():
                try:
                    _scan_once(self.store, self.watch_dir)
                except Exception:
                    log.exception("inbox: scan failed")
                _status["last_scan_at"] = _now_iso()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL)
                except asyncio.TimeoutError:
                    pass
        finally:
            _status["running"] = False
            log.info("inbox watcher stopped")

    def stop(self) -> None:
        self._stop.set()

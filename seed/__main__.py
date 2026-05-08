"""Запуск: ``python -m seed`` — наливает стартовый набор в `tasks.db`.

Идемпотентен: если в БД уже есть карточки с такими же title — пропустит.
"""
from __future__ import annotations

import sys

from kanban_store import Store

from .tasks import all_tasks


def run() -> None:
    store = Store()
    existing = {t.title for t in store.list_tasks()}
    skipped = 0
    created = 0
    for spec in all_tasks():
        if spec["title"] in existing:
            skipped += 1
            continue
        t = store.create_task(
            title=spec["title"],
            description=spec.get("description", ""),
            acceptance=spec.get("acceptance", ""),
            status=spec.get("status", "backlog"),
            priority=spec.get("priority", "normal"),
            size=spec.get("size", "M"),
            external_blocker=spec.get("external_blocker"),
            actor="user",
            links=spec.get("links") or None,
        )
        created += 1
        print(f"  + {t.id}  [{t.status:11s}] {t.title}")
    print(f"\nDone. Created: {created}, skipped (already present): {skipped}")
    # Snapshot после seed — пусть будет в гите как baseline
    fp = store.save_snapshot()
    print(f"Snapshot: {fp}")


if __name__ == "__main__":
    sys.exit(run())

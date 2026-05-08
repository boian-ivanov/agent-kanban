"""Generic seed: несколько примеров задач для свежей инсталляции.

Используется по умолчанию, если запустить ``python -m seed`` на пустой БД.
Реальный набор задач для своего проекта проще накидать через UI или
через ``kanban_data/inbox/``.
"""
from __future__ import annotations

from typing import Any


def all_tasks() -> list[dict[str, Any]]:
    return [
        {
            "title": "Изучить README и попробовать drag-drop",
            "description": "Открой http://localhost:7777/, перетащи карточку из Бэклога в Согласовано, кликни по карточке для деталей.",
            "acceptance": "Карточка перемещена; модалка открывается; сохраняются комментарий и ссылка.",
            "status": "backlog",
            "priority": "normal",
            "size": "S",
        },
        {
            "title": "Настроить inbox: положить .md в kanban_data/inbox/",
            "description": "Создай файл `test.md` с frontmatter (title/priority/size) в `kanban_data/inbox/` и проверь что через 5 сек появится карточка.",
            "acceptance": "Файл `kanban_data/inbox/test.md` пропал, перезжав в `processed/YYYY-MM-DD/`. Карточка появилась в Бэклоге.",
            "status": "backlog",
            "priority": "low",
            "size": "S",
        },
        {
            "title": "Настроить automation rules: rules.json",
            "description": "Открой `kanban_data/rules.json`, добавь правило `task_idle: status=done, days=14 → action: move_to cancelled`. Engine применит правило в течение `KANBAN_AUTOMATION_INTERVAL` секунд.",
            "acceptance": "Старые задачи в Закрыто (>14 дней) автоматически уходят в Отменено с комментарием от actor=automation.",
            "status": "backlog",
            "priority": "low",
            "size": "M",
        },
    ]

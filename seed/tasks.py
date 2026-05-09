"""Generic seed: a few example tasks for a fresh installation.

Used by default when ``python -m seed`` is run on an empty database.
The real backlog for your own project is easier to add through the UI
or via ``kanban_data/inbox/``.
"""
from __future__ import annotations

from typing import Any


def all_tasks() -> list[dict[str, Any]]:
    return [
        {
            "title": "Read README and try drag-drop",
            "description": "Open http://localhost:7777/, drag a card from Backlog into Approved, and click the card to inspect details.",
            "acceptance": "Card moves between columns; detail modal opens; saving a comment and a link both succeed.",
            "status": "backlog",
            "priority": "normal",
            "size": "S",
        },
        {
            "title": "Set up inbox: drop a .md file in kanban_data/inbox/",
            "description": "Create `test.md` with frontmatter (title/priority/size) inside `kanban_data/inbox/` and verify a card appears within 5 seconds.",
            "acceptance": "`kanban_data/inbox/test.md` is gone, having moved to `processed/YYYY-MM-DD/`. A new card is visible in Backlog.",
            "status": "backlog",
            "priority": "low",
            "size": "S",
        },
        {
            "title": "Configure automation rules: rules.json",
            "description": "Open `kanban_data/rules.json` and add a rule `task_idle: status=done, days=14 -> action: move_to cancelled`. The engine applies the rule within `KANBAN_AUTOMATION_INTERVAL` seconds.",
            "acceptance": "Old tasks in Done (>14 days) move to Cancelled automatically with a comment from actor=automation.",
            "status": "backlog",
            "priority": "low",
            "size": "M",
        },
    ]

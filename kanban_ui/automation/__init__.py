"""Background automation: inbox watcher + rule engine.

Подключаются в FastAPI app через lifespan (см. kanban_ui/main.py):
``inbox_watcher_task`` и ``rule_engine_task`` запускаются как
asyncio-задачи и отменяются при shutdown.
"""
from .inbox import InboxWatcher, inbox_status
from .rules import RuleEngine, rules_status, emit_rule_event
from .webhooks import (
    init_dispatcher,
    shutdown_dispatcher,
    emit_event,
    webhooks_status,
)
from . import plan_md

__all__ = [
    "InboxWatcher",
    "RuleEngine",
    "inbox_status",
    "rules_status",
    "emit_rule_event",
    "init_dispatcher",
    "shutdown_dispatcher",
    "emit_event",
    "webhooks_status",
    "plan_md",
]

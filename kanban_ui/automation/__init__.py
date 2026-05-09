"""Background automation: inbox watcher + rule engine.

Wired into the FastAPI app through the lifespan (see kanban_ui/main.py):
``inbox_watcher_task`` and ``rule_engine_task`` are started as asyncio
tasks and cancelled on shutdown.
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

"""T-314: reactive rule dispatch fires each matching rule exactly once.

Regression for the ``emit_rule_event`` double-dispatch bug: the event loop
body was duplicated, so every matching rule applied twice per event — a
task_moved ``in_progress -> testing`` rule would have spawned two verifier
processes per arrival. Also covers the from_status / project_id filters the
verifier rules rely on.
"""

from __future__ import annotations

import json
from pathlib import Path

from kanban_store import Store
from kanban_ui.automation.rules import RuleEngine, emit_rule_event

VERIFY_RULE = {
    "name": "Verify tasks arriving in testing",
    "enabled": True,
    "trigger": {
        "type": "task_moved",
        "to_status": "testing",
        "from_status": "in_progress",
        "project_id": "agent-kanban",
    },
    "action": {"type": "add_comment", "comment": "verifier dispatched"},
}


def _write_rules(tmp_path: Path, rules: list[dict]) -> Path:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"rules": rules}), encoding="utf-8")
    return path


def _event(task_id: str, from_status: str, to_status: str, project_id: str) -> dict:
    return {
        "task": {
            "id": task_id,
            "title": "t",
            "project_id": project_id,
            "status": to_status,
        },
        "from_status": from_status,
        "to_status": to_status,
    }


def _comments(store: Store, task_id: str) -> list[str]:
    return [
        h.comment
        for h in store.get_task(task_id).history
        if h.action == "comment"
    ]


def test_task_moved_rule_fires_exactly_once(tmp_path):
    """Regression: the duplicated emit_rule_event loop applied the rule twice;
    a single in_progress -> testing move must dispatch the verifier once."""
    store = Store(tmp_path / "t.db")
    t = store.create_task("Verify me", status="testing", project_id="agent-kanban")
    RuleEngine(store, _write_rules(tmp_path, [VERIFY_RULE]))

    emit_rule_event(
        "task_moved", _event(t.id, "in_progress", "testing", "agent-kanban")
    )

    assert _comments(store, t.id) == ["verifier dispatched"]


def test_task_moved_ignores_other_transitions(tmp_path):
    """from_status filter: only in_progress -> testing triggers the verifier."""
    store = Store(tmp_path / "t.db")
    t = store.create_task("Verify me", status="testing", project_id="agent-kanban")
    RuleEngine(store, _write_rules(tmp_path, [VERIFY_RULE]))

    emit_rule_event(
        "task_moved", _event(t.id, "analyst", "testing", "agent-kanban")
    )
    emit_rule_event("task_moved", _event(t.id, "in_progress", "uat", "agent-kanban"))

    assert _comments(store, t.id) == []


def test_task_moved_respects_project_filter(tmp_path):
    """project_id filter: other projects must not dispatch this project's
    verifier rule."""
    store = Store(tmp_path / "t.db")
    t = store.create_task("Verify me", status="testing", project_id="agent-kanban")
    RuleEngine(store, _write_rules(tmp_path, [VERIFY_RULE]))

    emit_rule_event(
        "task_moved", _event(t.id, "in_progress", "testing", "salon-platform")
    )
    emit_rule_event(
        "task_moved", _event(t.id, "in_progress", "testing", "agent-kanban")
    )

    assert _comments(store, t.id) == ["verifier dispatched"]


def test_task_commented_prefix_still_fires_once(tmp_path):
    """The task_commented path keeps working after the loop dedup."""
    store = Store(tmp_path / "t.db")
    t = store.create_task("Steer me", project_id="agent-kanban")
    rule = {
        "name": "Steer via @agent",
        "enabled": True,
        "trigger": {"type": "task_commented", "prefix": "@agent",
                    "project_id": "agent-kanban"},
        "action": {"type": "add_comment", "comment": "steered"},
    }
    RuleEngine(store, _write_rules(tmp_path, [rule]))

    emit_rule_event(
        "task_commented",
        {"task": {"id": t.id, "project_id": "agent-kanban"},
         "comment": "@agent please steer", "comment_id": 1},
    )

    assert _comments(store, t.id) == ["steered"]

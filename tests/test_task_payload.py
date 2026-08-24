"""Task payload enrichment: comments/ancestors/children_count, board
include=full, and the filtered /api/tasks list endpoint.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kanban_store.store import Store
from kanban_ui import main


def _set_parent(store: Store, child: str, parent: str, kind: str = "task") -> None:
    """Parent a task via SQL — the REST API does not expose hierarchy writes yet."""
    with store._lock:
        store._conn.execute(
            "UPDATE tasks SET parent_id=?, kind=? WHERE id=?",
            (parent, kind, child),
        )


def _hierarchy(store: Store):
    epic = store.create_task("Epic", description="epic desc", acceptance="epic acc")
    story = store.create_task("Story", description="story desc")
    task = store.create_task("Ticket", description="ticket desc")
    _set_parent(store, story.id, epic.id, "story")
    _set_parent(store, task.id, story.id, "task")
    return epic, story, task


# ---------------------------------------------------------------------------
# Store level
# ---------------------------------------------------------------------------


def test_task_payload_comments_ancestors_children(tmp_path):
    store = Store(tmp_path / "t.db")
    epic, story, task = _hierarchy(store)
    store.add_comment(task.id, "first note", actor="agent:fe")
    store.add_comment(task.id, "second note", actor="user")
    store.move_task(task.id, "in_progress", actor="agent:fe")

    pub = store.get_task(task.id).to_public()
    assert pub["parent_id"] == story.id
    assert pub["kind"] == "task"
    # comments: only action='comment' history rows, chronological
    assert [(c["actor"], c["text"]) for c in pub["comments"]] == [
        ("agent:fe", "first note"),
        ("user", "second note"),
    ]
    # a move is history, not a comment
    assert len(pub["comments"]) == 2
    assert any(h["action"] == "move" for h in pub["history"])
    # ancestors: root (epic) first, with titles + description + acceptance
    assert [a["id"] for a in pub["ancestors"]] == [epic.id, story.id]
    assert pub["ancestors"][0]["kind"] == "task"
    assert pub["ancestors"][0]["title"] == "Epic"
    assert pub["ancestors"][0]["description"] == "epic desc"
    assert pub["ancestors"][0]["acceptance"] == "epic acc"
    assert pub["ancestors"][1]["title"] == "Story"
    assert pub["ancestors"][1]["description"] == "story desc"
    # children_count
    assert pub["children_count"] == 0
    assert store.get_task(story.id).to_public()["children_count"] == 1
    assert store.get_task(epic.id).to_public()["children_count"] == 1


def test_list_tasks_filters(tmp_path):
    store = Store(tmp_path / "t.db")
    a = store.create_task("A", status="backlog", assignee="agent:fe")
    b = store.create_task("B", status="analyst", assignee="agent:be")
    c = store.create_task("C", status="analyst", assignee="agent:fe")
    store.create_task("D", status="done")
    _set_parent(store, c.id, a.id)
    store.add_comment(a.id, "later note", actor="user")

    # list_tasks orders by (status, column_order) — compare as sets
    assert {t.id for t in store.list_tasks(status="analyst")} == {b.id, c.id}
    assert {t.id for t in store.list_tasks(assignee="agent:fe")} == {a.id, c.id}
    assert [t.id for t in store.list_tasks(status="analyst", assignee="agent:fe")] == [
        c.id
    ]
    assert [t.id for t in store.list_tasks(parent_id=a.id)] == [c.id]
    # updated_since matches tasks with a history entry at/after the timestamp
    since = "2099-01-01T00:00:00+00:00"  # pinned: strictly after every create ts
    with store._lock:
        store._conn.execute(
            "UPDATE task_history SET ts=? WHERE task_id=? AND action='comment'",
            (since, a.id),
        )
    assert [t.id for t in store.list_tasks(updated_since=since)] == [a.id]
    assert len(store.list_tasks(updated_since="2000-01-01T00:00:00+00:00")) == 4


# ---------------------------------------------------------------------------
# API level
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_path, monkeypatch):
    # other test files leave KANBAN_DEFAULT_PROJECT_ID polluted — pin it
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    store = Store(tmp_path / "api.db")
    monkeypatch.setattr(main, "_store", store)
    return TestClient(main.app), store


def test_get_task_payload(api):
    client, store = api
    epic, story, task = _hierarchy(store)
    store.add_comment(task.id, "hello", actor="agent:fe")
    r = client.get(f"/api/tasks/{task.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["comments"] == [
        {
            "id": body["comments"][0]["id"],
            "ts": body["comments"][0]["ts"],
            "actor": "agent:fe",
            "text": "hello",
        }
    ]
    assert [a["id"] for a in body["ancestors"]] == [epic.id, story.id]
    assert body["ancestors"][0]["title"] == "Epic"
    assert body["children_count"] == 0
    assert body["parent_id"] == story.id


def test_board_default_summary_and_full(api):
    client, store = api
    epic = store.create_task("Epic", description="epic desc", acceptance="epic acc")
    story = store.create_task("Story")
    _set_parent(store, story.id, epic.id, "story")
    store.add_comment(story.id, "note", actor="user")

    summary = client.get("/api/board?project=default").json()
    card = summary["tasks"]["backlog"][0]
    assert "description" not in card
    assert "comments" not in card

    full = client.get("/api/board?project=default&include=full").json()
    by_id = {c["id"]: c for c in full["tasks"]["backlog"]}
    epic_card = by_id[epic.id]
    assert epic_card["description"] == "epic desc"
    assert epic_card["acceptance"] == "epic acc"
    assert epic_card["children_count"] == 1
    story_card = by_id[story.id]
    assert story_card["parent_id"] == epic.id
    assert story_card["comments"] == [
        {
            "id": story_card["comments"][0]["id"],
            "ts": story_card["comments"][0]["ts"],
            "actor": "user",
            "text": "note",
        }
    ]
    assert story_card["ancestors"][0]["title"] == "Epic"


def test_filtered_list_endpoint(api):
    client, store = api
    a = store.create_task("A", status="backlog", assignee="agent:fe")
    b = store.create_task("B", status="analyst", assignee="agent:be")
    _set_parent(store, b.id, a.id)
    store.add_comment(b.id, "comment", actor="user")

    r = client.get("/api/tasks?project=default&status=analyst")
    assert r.status_code == 200
    assert [t["id"] for t in r.json()["tasks"]] == [b.id]
    assert r.json()["count"] == 1

    r = client.get("/api/tasks?assignee=agent%3Afe")
    assert [t["id"] for t in r.json()["tasks"]] == [a.id]

    r = client.get(f"/api/tasks?parent_id={a.id}")
    assert [t["id"] for t in r.json()["tasks"]] == [b.id]

    r = client.get("/api/tasks?updated_since=2000-01-01T00%3A00%3A00%2B00%3A00")
    assert r.json()["count"] == 2

    r = client.get("/api/tasks?status=done")
    assert r.json()["tasks"] == []

    # summary fields include hierarchy info
    card = client.get("/api/tasks?project=default").json()["tasks"][0]
    assert "parent_id" in card and "kind" in card and "status" in card

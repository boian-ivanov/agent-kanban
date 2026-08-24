"""Per-task driver plumbing: atomic claim, task_chat messages, task_runs.

Covers the endpoints the examples/task-driver.py drives:
  POST /api/tasks/{id}/claim  (assignee=agent:<role> + approved→in_progress)
  GET/POST /api/tasks/{id}/chat
  GET/POST /api/tasks/{id}/runs
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kanban_store.store import Store
from kanban_ui import main

# ---------------------------------------------------------------------------
# Store level
# ---------------------------------------------------------------------------


def test_claim_task_atomic_assign_and_move(tmp_path):
    store = Store(tmp_path / "t.db")
    t = store.create_task("Claim me", status="approved")
    claimed = store.claim_task(t.id, "agent:fe", actor="omp")

    assert claimed.status == "in_progress"
    assert claimed.assignee == "agent:fe"
    actions = [(h.action, h.from_status, h.to_status) for h in claimed.history]
    assert ("move", "approved", "in_progress") in actions
    assert ("assign", None, None) in actions


def test_claim_task_idempotent_same_assignee(tmp_path):
    store = Store(tmp_path / "t.db")
    t = store.create_task("Claim me", status="approved")
    store.claim_task(t.id, "agent:fe", actor="omp")
    again = store.claim_task(t.id, "agent:fe", actor="omp")  # no-op, no error

    assert again.status == "in_progress"
    assert again.assignee == "agent:fe"
    # only one claim round recorded
    moves = [h for h in again.history if h.action == "move"]
    assert len(moves) == 1


def test_claim_task_conflicts(tmp_path):
    store = Store(tmp_path / "t.db")
    t = store.create_task("Claim me", status="approved")
    store.claim_task(t.id, "agent:fe", actor="omp")

    # owned by someone else
    with pytest.raises(RuntimeError):
        store.claim_task(t.id, "agent:be", actor="omp")
    # not claimable from a later status
    store.move_task(t.id, "testing", actor="agent:fe")
    with pytest.raises(RuntimeError):
        store.claim_task(t.id, "agent:fe", actor="omp")
    # unknown task
    with pytest.raises(KeyError):
        store.claim_task("T-999", "agent:fe", actor="omp")


def test_chat_messages_append_seq(tmp_path):
    store = Store(tmp_path / "t.db")
    t = store.create_task("Chatty")
    m1 = store.add_chat_message(t.id, "agent:fe", "first message")
    m2 = store.add_chat_message(t.id, "agent:fe", "second message")
    m3 = store.add_chat_message(t.id, "user", "question")

    assert (m1["seq"], m1["content"]) == (1, "first message")
    assert (m2["seq"], m2["content"]) == (2, "second message")
    assert (m3["seq"], m3["content"]) == (3, "question")
    assert [m["content"] for m in store.get_chat(t.id)] == [
        "first message",
        "second message",
        "question",
    ]
    with pytest.raises(KeyError):
        store.add_chat_message("T-999", "agent:fe", "nope")


def test_register_run_upsert(tmp_path):
    store = Store(tmp_path / "t.db")
    t = store.create_task("Run me")
    run = store.register_run(
        t.id,
        pid=4242,
        started_at="2026-08-24T00:00:00+00:00",
        model="opencode-go/deepseek-v4-flash",
        role="fe",
        control_port=55123,
        status="running",
    )
    assert run["pid"] == 4242
    assert run["control_port"] == 55123
    assert run["status"] == "running"

    # finish update keeps the identity fields
    done = store.register_run(t.id, ended_at="2026-08-24T01:00:00+00:00", status="done")
    assert done["pid"] == 4242
    assert done["ended_at"] == "2026-08-24T01:00:00+00:00"
    assert done["status"] == "done"

    with pytest.raises(KeyError):
        store.register_run("T-999", pid=1)
    with pytest.raises(ValueError):
        store.register_run(t.id)


# ---------------------------------------------------------------------------
# API level
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    store = Store(tmp_path / "api.db")
    monkeypatch.setattr(main, "_store", store)
    return TestClient(main.app), store


def test_claim_endpoint(api):
    client, store = api
    t = store.create_task("Claim me", status="approved")

    r = client.post(f"/api/tasks/{t.id}/claim", json={"assignee": "agent:fe"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "in_progress"
    assert body["assignee"] == "agent:fe"
    assert [h["action"] for h in body["history"]].count("move") == 1

    # idempotent re-claim by the same assignee
    r2 = client.post(f"/api/tasks/{t.id}/claim", json={"assignee": "agent:fe"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "in_progress"

    # conflict: different assignee
    r3 = client.post(f"/api/tasks/{t.id}/claim", json={"assignee": "agent:be"})
    assert r3.status_code == 409

    # not claimable from a later status
    store.move_task(t.id, "testing", actor="agent:fe")
    r4 = client.post(f"/api/tasks/{t.id}/claim", json={"assignee": "agent:fe"})
    assert r4.status_code == 409

    assert (
        client.post("/api/tasks/T-999/claim", json={"assignee": "agent:fe"}).status_code
        == 404
    )


def test_chat_endpoints(api):
    client, store = api
    t = store.create_task("Chatty")

    r1 = client.post(
        f"/api/tasks/{t.id}/chat",
        json={"role": "agent:fe", "content": "hello"},
    )
    assert r1.status_code == 201
    assert r1.json()["seq"] == 1
    r2 = client.post(
        f"/api/tasks/{t.id}/chat",
        json={"role": "agent:fe", "content": "world"},
    )
    assert r2.json()["seq"] == 2

    msgs = client.get(f"/api/tasks/{t.id}/chat").json()["messages"]
    assert [m["content"] for m in msgs] == ["hello", "world"]
    assert (
        client.post(
            "/api/tasks/T-999/chat", json={"role": "agent:fe", "content": "x"}
        ).status_code
        == 404
    )
    assert client.get("/api/tasks/T-999/chat").status_code == 404


def test_runs_endpoints(api):
    client, store = api
    t = store.create_task("Run me")

    r = client.post(
        f"/api/tasks/{t.id}/runs",
        json={
            "pid": 4242,
            "started_at": "2026-08-24T00:00:00+00:00",
            "model": "opencode-go/deepseek-v4-flash",
            "role": "fe",
            "control_port": 55123,
            "status": "running",
        },
    )
    assert r.status_code == 200
    run = r.json()["run"]
    assert run["pid"] == 4242 and run["control_port"] == 55123

    done = client.post(
        f"/api/tasks/{t.id}/runs",
        json={"ended_at": "2026-08-24T01:00:00+00:00", "status": "done"},
    ).json()["run"]
    assert done["pid"] == 4242 and done["status"] == "done"

    fetched = client.get(f"/api/tasks/{t.id}/runs").json()["run"]
    assert fetched == done
    assert client.post("/api/tasks/T-999/runs", json={"pid": 1}).status_code == 404
    assert client.post(f"/api/tasks/{t.id}/runs", json={}).status_code == 400

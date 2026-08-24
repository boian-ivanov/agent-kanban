"""MCP stdio parity — kanban_mcp/server.py mirrors the agent-facing REST API.

Covers the T-315 parity tools: kanban_context / kanban_children /
kanban_subtree / kanban_claim / kanban_chat+kanban_send_chat /
kanban_run+kanban_register_run / kanban_stop+kanban_steer, the extended
kanban_list filters (parent_id / updated_since) and kanban_create
(parent_id / kind). All tools run against an isolated scratch DB.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

import kanban_mcp.server as mcp
from kanban_store import Store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Scratch store on a temp DB, wired into the MCP module singleton."""
    db = tmp_path / "mcp-test.db"
    monkeypatch.setenv("KANBAN_DB", str(db))
    mcp._store = None  # re-point the cached singleton at the scratch DB
    s = mcp._get_store()
    yield s
    mcp._store = None


def _hierarchy(s: Store):
    """epic → story → task; returns (epic, story, task)."""
    epic = s.create_task(title="Epic", kind="epic", actor="test")
    story = s.create_task(title="Story", kind="story", parent_id=epic.id, actor="test")
    task = s.create_task(title="Ticket", kind="task", parent_id=story.id, actor="test")
    return epic, story, task


# ---------------------------------------------------------------------------
# list filters
# ---------------------------------------------------------------------------


def test_list_filters_parent_and_updated_since(store):
    epic, story, task = _hierarchy(store)
    other = store.create_task(title="Unrelated", actor="test")

    by_parent = mcp.kanban_list(parent_id=story.id)
    assert by_parent["ok"] is True
    assert [t["id"] for t in by_parent["data"]["tasks"]] == [task.id]

    all_tasks = mcp.kanban_list()
    ids = {t["id"] for t in all_tasks["data"]["tasks"]}
    assert {epic.id, story.id, other.id} <= ids

    # updated_since matches only tasks with a history entry at/after the ts
    before = mcp.kanban_list(updated_since="9999-12-31T00:00:00+00:00")
    assert before["data"]["count"] == 0
    after = mcp.kanban_list(updated_since="2000-01-01T00:00:00+00:00")
    assert after["data"]["count"] == len(ids)


def test_list_summary_shape_matches_rest(store):
    epic, story, _task = _hierarchy(store)
    r = mcp.kanban_list(parent_id=epic.id)
    card = r["data"]["tasks"][0]
    assert card["id"] == story.id
    # same shape as the REST /api/tasks summary (main._task_summary)
    for field in ("id", "title", "status", "priority", "size", "assignee",
                  "external_blocker", "blockers", "parent_id", "kind",
                  "moved_at", "created_at", "project_id"):
        assert field in card, f"missing {field}"


# ---------------------------------------------------------------------------
# context / children / subtree
# ---------------------------------------------------------------------------


def test_context_bundle(store):
    _epic, _story, task = _hierarchy(store)
    store.move_task(task.id, "approved", actor="test")
    store.add_comment(task.id, "scoping note", actor="test")

    r = mcp.kanban_context(task.id)
    assert r["ok"] is True
    d = r["data"]
    assert d["task_id"] == task.id
    assert d["task"]["title"] == "Ticket"
    # ancestor chain: epic → story
    kinds = [a["kind"] for a in d["ancestors"]]
    assert kinds == ["epic", "story"], d["ancestors"]
    # recent comments
    assert d["comments"][-1]["text"] == "scoping note"
    # children summary of the story's parent view
    assert d["children"] == []
    # constraints from examples/agents.json
    assert isinstance(d["constraints"], list)

    assert mcp.kanban_context("NOPE")["ok"] is False


def test_children_summary_and_full(store):
    _epic, story, task = _hierarchy(store)
    s = mcp.kanban_children(story.id)
    assert s["ok"] is True
    assert s["data"]["count"] == 1
    child = s["data"]["children"][0]
    assert child["id"] == task.id and "description" not in child

    f = mcp.kanban_children(story.id, include="full")
    assert f["data"]["children"][0]["description"] == ""
    assert f["data"]["children"][0]["acceptance"] == ""

    assert mcp.kanban_children(story.id, include="bogus")["ok"] is False
    assert mcp.kanban_children("NOPE")["ok"] is False


def test_subtree_recursive(store):
    epic, story, task = _hierarchy(store)
    r = mcp.kanban_subtree(epic.id)
    assert r["ok"] is True
    tree = r["data"]["tree"]
    assert len(tree) == 1
    story_node = tree[0]
    assert story_node["id"] == story.id and story_node["kind"] == "story"
    assert [c["id"] for c in story_node["children"]] == [task.id]
    assert mcp.kanban_subtree("NOPE")["ok"] is False


# ---------------------------------------------------------------------------
# claim / chat / runs
# ---------------------------------------------------------------------------


def test_claim(store):
    _, _, task = _hierarchy(store)
    # claim requires approved/in_progress
    assert mcp.kanban_claim(task.id, "agent:fe")["ok"] is False
    store.move_task(task.id, "approved", actor="test")

    r = mcp.kanban_claim(task.id, "agent:fe")
    assert r["ok"] is True
    assert r["data"]["status"] == "in_progress"
    assert r["data"]["assignee"] == "agent:fe"

    # conflict: other assignee
    conflict = mcp.kanban_claim(task.id, "agent:be")
    assert conflict["ok"] is False and "already assigned" in conflict["error"]
    # idempotent re-claim by the same assignee
    again = mcp.kanban_claim(task.id, "agent:fe")
    assert again["ok"] is True
    # missing task
    assert mcp.kanban_claim("NOPE", "agent:fe")["ok"] is False


def test_chat_roundtrip(store):
    _, _, task = _hierarchy(store)
    sent = mcp.kanban_send_chat(task.id, "agent:fe", "hello driver")
    assert sent["ok"] is True
    assert sent["data"]["seq"] == 1

    got = mcp.kanban_chat(task.id)
    assert got["ok"] is True
    msgs = got["data"]["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "agent:fe" and msgs[0]["content"] == "hello driver"

    assert mcp.kanban_chat("NOPE")["ok"] is False
    assert mcp.kanban_send_chat("NOPE", "u", "x")["ok"] is False


def test_run_roundtrip(store):
    _, _, task = _hierarchy(store)
    assert mcp.kanban_run(task.id)["data"]["run"] is None

    reg = mcp.kanban_register_run(
        task.id, pid=1234, model="m", role="fe", status="running", control_port=9999
    )
    assert reg["ok"] is True
    run = reg["data"]["run"]
    assert run["pid"] == 1234 and run["control_port"] == 9999

    got = mcp.kanban_run(task.id)["data"]["run"]
    assert got["status"] == "running"

    # partial upsert (driver exit)
    done = mcp.kanban_register_run(task.id, status="done", tokens_used=7)
    assert done["data"]["run"]["status"] == "done"
    assert done["data"]["run"]["tokens_used"] == 7
    assert done["data"]["run"]["pid"] == 1234  # untouched field

    assert mcp.kanban_register_run("NOPE", pid=1)["ok"] is False
    assert mcp.kanban_run("NOPE")["ok"] is False


# ---------------------------------------------------------------------------
# stop / steer (control socket)
# ---------------------------------------------------------------------------


class _FakeDriver:
    """One-shot control socket that records the command and acks it."""

    def __init__(self):
        self.received = {}
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        conn, _ = self._srv.accept()
        try:
            data = conn.recv(65536)
            self.received = json.loads(data.decode())
            conn.sendall(
                (json.dumps({"ok": True, "ack": self.received.get("cmd")}) + "\n").encode()
            )
        finally:
            conn.close()
            self._srv.close()

    def join(self, timeout=5):
        self._t.join(timeout)


def _register_live_run(store, task_id, port):
    store.register_run(
        task_id, pid=42, role="fe", status="running", control_port=port
    )


def test_stop_relays_to_driver(store):
    _, _, task = _hierarchy(store)
    fake = _FakeDriver()
    _register_live_run(store, task.id, fake.port)

    r = mcp.kanban_stop(task.id, reason="budget breach", to_status="approved")
    fake.join()
    assert r["ok"] is True
    assert fake.received["cmd"] == "stop"
    assert fake.received["reason"] == "budget breach"
    assert fake.received["to_status"] == "approved"


def test_steer_relays_to_driver(store):
    _, _, task = _hierarchy(store)
    fake = _FakeDriver()
    _register_live_run(store, task.id, fake.port)

    r = mcp.kanban_steer(task.id, text="you are the only live session")
    fake.join()
    assert r["ok"] is True
    assert fake.received["cmd"] == "steer"
    assert fake.received["text"] == "you are the only live session"


def test_stop_steer_error_paths(store):
    _, _, task = _hierarchy(store)
    # no run registered
    assert mcp.kanban_stop(task.id, reason="x", to_status="approved")["ok"] is False
    assert mcp.kanban_steer(task.id, text="x")["ok"] is False
    # run registered but not live
    store.register_run(task.id, pid=1, status="done", control_port=9999)
    assert mcp.kanban_stop(task.id, reason="x", to_status="approved")["ok"] is False
    # invalid to_status
    assert mcp.kanban_stop(task.id, reason="x", to_status="bogus")["ok"] is False
    # missing task
    assert mcp.kanban_stop("NOPE", reason="x")["ok"] is False
    assert mcp.kanban_steer("NOPE", text="x")["ok"] is False
    # live run but socket dead → ControlUnavailable surfaced
    store.register_run(task.id, pid=1, status="running", control_port=1)
    r = mcp.kanban_stop(task.id, reason="x", to_status="approved")
    assert r["ok"] is False and "control" in r["error"].lower()


# ---------------------------------------------------------------------------
# create with parent_id / kind
# ---------------------------------------------------------------------------


def test_create_hierarchy_parity(store):
    epic, story, _ = _hierarchy(store)
    r = mcp.kanban_create(
        title="Ticket 2", parent_id=story.id, kind="task", project_id="default"
    )
    assert r["ok"] is True
    assert r["data"]["parent_id"] == story.id and r["data"]["kind"] == "task"

    # child kind is forced by the parent (epic → story): passing the wrong
    # kind errors, same as REST
    bad_kind = mcp.kanban_create(title="Wrong", parent_id=epic.id, kind="task")
    assert bad_kind["ok"] is False
    forced = mcp.kanban_create(title="Story 2", parent_id=epic.id, kind="story")
    assert forced["ok"] is True and forced["data"]["kind"] == "story"

    # epics are top-level
    bad = mcp.kanban_create(title="Bad", parent_id=epic.id, kind="epic")
    assert bad["ok"] is False


def test_constraints_read_from_agents_json():
    c = mcp._constraints()
    assert isinstance(c, list)
    assert any("git commit" in x for x in c)

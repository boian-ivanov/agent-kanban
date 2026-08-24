"""T-311: agent stop/steer — endpoints, task_commented rule trigger, driver.

Covers:
  POST /api/tasks/{id}/agent/stop|steer (relay to the driver control socket)
  GET  /api/tasks/{id}?since_seq=N       (driver comment poll)
  rules engine: task_commented trigger + agent_steer action
  examples/task-driver.py: classify_steer + ControlServer handlers
"""

from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kanban_store.store import Store
from kanban_ui import main
from kanban_ui.automation import rules

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_driver():
    spec = importlib.util.spec_from_file_location(
        "task_driver", REPO_ROOT / "examples" / "task-driver.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


driver = _load_driver()

# ---------------------------------------------------------------------------
# Store level
# ---------------------------------------------------------------------------


def test_add_comment_returns_history_id(tmp_path):
    store = Store(tmp_path / "t.db")
    t = store.create_task("Commented")
    c1 = store.add_comment(t.id, "first", actor="user")
    c2 = store.add_comment(t.id, "second", actor="user")
    assert c2 > c1 > 0

    # get_history_since filters by id (comment poll cursor)
    later = store.get_history_since(t.id, c1)
    assert [h.id for h in later] == [c2]
    assert later[0].comment == "second"
    assert store.get_history_since(t.id, c2) == []


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


def test_since_seq_filters_task_payload(api):
    client, store = api
    t = store.create_task("Poll me")
    store.add_comment(t.id, "old", actor="user")
    cid = store.add_comment(t.id, "@agent focus on X", actor="user")

    full = client.get(f"/api/tasks/{t.id}").json()
    assert [c["id"] for c in full["comments"]] == [cid - 1, cid]

    polled = client.get(f"/api/tasks/{t.id}?since_seq={cid - 1}").json()
    assert [c["id"] for c in polled["comments"]] == [cid]
    assert [h["id"] for h in polled["history"]] == [cid]
    # since_seq beyond the last row -> empty history
    assert client.get(f"/api/tasks/{t.id}?since_seq={cid}").json()["history"] == []


def test_stop_endpoint_validation_and_no_live_run(api):
    client, store = api
    t = store.create_task("Stop me")

    # unknown task
    assert (
        client.post("/api/tasks/T-999/agent/stop", json={"reason": "x"}).status_code
        == 404
    )
    # no run registered
    r = client.post(f"/api/tasks/{t.id}/agent/stop", json={"reason": "x"})
    assert r.status_code == 409
    # bad to_status
    store.register_run(t.id, pid=1, status="running", control_port=12345)
    r = client.post(
        f"/api/tasks/{t.id}/agent/stop", json={"reason": "x", "to_status": "bogus"}
    )
    assert r.status_code == 400
    # run registered but finished
    store.register_run(t.id, status="done")
    assert (
        client.post(f"/api/tasks/{t.id}/agent/stop", json={"reason": "x"}).status_code
        == 409
    )


def test_stop_endpoint_unreachable_driver(api):
    client, store = api
    t = store.create_task("Stop me")
    # control_port on a dead port -> 502, no crash
    store.register_run(t.id, pid=1, status="running", control_port=1)
    r = client.post(f"/api/tasks/{t.id}/agent/stop", json={"reason": "scope too big"})
    assert r.status_code == 502
    assert "unreachable" in r.json()["detail"]


def test_steer_endpoint_validation_and_unreachable(api):
    client, store = api
    t = store.create_task("Steer me")

    assert (
        client.post(f"/api/tasks/{t.id}/agent/steer", json={"text": "x"}).status_code
        == 409
    )
    # empty text rejected by pydantic (min_length=1)
    store.register_run(t.id, pid=1, status="running", control_port=1)
    assert (
        client.post(f"/api/tasks/{t.id}/agent/steer", json={"text": ""}).status_code
        == 422
    )
    r = client.post(f"/api/tasks/{t.id}/agent/steer", json={"text": "focus on X"})
    assert r.status_code == 502
    assert "unreachable" in r.json()["detail"]


def test_stop_steer_relay_to_live_driver(api):
    """A reachable control socket receives the exact command."""
    import threading

    client, store = api
    t = store.create_task("Relay")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    store.register_run(t.id, pid=1, status="running", control_port=port)

    received: dict = {}

    def accept_once():
        conn, _ = listener.accept()
        with conn:
            data = conn.recv(65536)
            received["req"] = json.loads(data.decode())
            conn.sendall(b'{"ok": true}\n')

    # accept concurrently: the endpoint blocks until the driver replies
    thread = threading.Thread(target=accept_once)
    thread.start()
    r = client.post(
        f"/api/tasks/{t.id}/agent/stop", json={"reason": "bye", "to_status": "approved"}
    )
    assert r.status_code == 200
    thread.join(timeout=5)
    assert received["req"] == {"cmd": "stop", "reason": "bye", "to_status": "approved"}

    thread = threading.Thread(target=accept_once)
    thread.start()
    r = client.post(f"/api/tasks/{t.id}/agent/steer", json={"text": "continue"})
    assert r.status_code == 200
    thread.join(timeout=5)
    assert received["req"] == {"cmd": "steer", "text": "continue", "comment_id": None}

    listener.close()


# ---------------------------------------------------------------------------
# Rules engine: task_commented trigger + agent_steer action
# ---------------------------------------------------------------------------


def test_rules_validate_task_commented_and_agent_steer(tmp_path):
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "steer",
                        "trigger": {"type": "task_commented", "prefix": "@agent"},
                        "action": {"type": "agent_steer"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rules_list, errs = rules._load_rules(rules_file)
    assert errs == []
    assert len(rules_list) == 1

    # bad prefix type -> validation error
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "bad",
                        "trigger": {"type": "task_commented", "prefix": 3},
                        "action": {"type": "agent_steer"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _, errs = rules._load_rules(bad)
    assert any("prefix" in e for e in errs)


def test_emit_task_commented_steers_or_logs(tmp_path):
    store = Store(tmp_path / "t.db")
    t = store.create_task("Steerable")
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "steer",
                        "trigger": {"type": "task_commented", "prefix": "@agent"},
                        "action": {"type": "agent_steer"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rules.RuleEngine(store, rules_file)
    payload = {
        "task": t.to_public(),
        "project": None,
        "comment": "ignored, no prefix",
        "comment_id": 1,
    }
    rules.emit_rule_event("task_commented", payload)
    assert rules._status["last_errors"] == []  # prefix filter: no match

    # no live run -> ControlUnavailable is caught and logged, no crash
    payload["comment"] = "@agent focus on X"
    payload["comment_id"] = 2
    rules.emit_rule_event("task_commented", payload)
    assert rules._status["last_errors"]  # logged
    assert any("no live agent run" in e["error"] for e in rules._status["last_errors"])


# ---------------------------------------------------------------------------
# Driver: classify_steer + ControlServer
# ---------------------------------------------------------------------------


def test_classify_steer():
    assert driver.classify_steer("@agent stop: scope too big") == (
        "stop",
        "blocked",
        "scope too big",
    )
    assert driver.classify_steer("@agent stop approved: retry") == (
        "stop",
        "approved",
        "retry",
    )
    assert driver.classify_steer("@agent stop blocked: human needed") == (
        "stop",
        "blocked",
        "human needed",
    )
    assert driver.classify_steer("@agent stop") == (
        "stop",
        "blocked",
        "stopped by user",
    )
    assert driver.classify_steer("@agent focus on X") == ("steer", "", "focus on X")
    assert driver.classify_steer("plain api steer") == ("steer", "", "plain api steer")
    assert driver.classify_steer("@agent") is None
    assert driver.classify_steer("") is None


def test_control_server_stop_and_steer():
    control = driver.ControlServer(task_id="T-1", model="m", role="default", pid=42)

    def send(payload: dict) -> dict:
        with socket.create_connection(("127.0.0.1", control.port), timeout=5) as s:
            s.sendall((json.dumps(payload) + "\n").encode())
            return json.loads(s.recv(65536).decode())

    assert send({"cmd": "ping"})["status"] == "running"
    assert send({"cmd": "budget"})["error"] == "not_implemented"

    assert send({"cmd": "steer", "text": "@agent go slower", "comment_id": 7})["ok"]
    assert send({"cmd": "steer", "text": "   "})["ok"] is False  # empty rejected
    assert control.next_steer() == ("@agent go slower", 7)
    assert control.next_steer() is None

    assert send({"cmd": "stop", "reason": "runaway", "to_status": "approved"})["ok"]
    assert control.get_stop_request() == {"reason": "runaway", "to_status": "approved"}
    assert send({"cmd": "ping"})["status"] == "stopping"

    control.close()

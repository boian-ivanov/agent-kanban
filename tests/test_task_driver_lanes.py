"""Worktree lanes: one worktree per card, so two agents never share a tree.

Why these tests exist (each defends a way lanes silently break):

- **Identity is the branch** ``task/<task_id>`` and ``git worktree list`` is
  the only ledger — so a re-dispatched card (budget breach, dead agent,
  verifier FAIL -> approved) must RESUME its lane instead of starting clean
  and discarding the previous attempt's work.
- **Allocation must be atomic** (``fcntl.flock``): two dispatches seconds
  apart must never both see lane 1 free.
- **A verifier must never create the tree it grades** — otherwise "PASS"
  refers to a tree nobody wrote.
- **No lane config means the shared project tree**, byte-identical to the
  pre-lane pipeline. That is the compatibility guarantee, and the driver
  test asserts it end to end (not just in the resolver).

Unit cases call the module's helpers directly (fast, deterministic); the
end-to-end cases run the real driver as a subprocess against a fake board
and a fake omp, exactly like tests/test_task_driver_watchdog.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER = REPO_ROOT / "examples" / "task-driver.py"
AGENT_LOG_DIR = REPO_ROOT / "kanban_data" / "agent-logs"


def load_driver():
    """Import examples/task-driver.py (hyphenated name) as a module."""
    spec = importlib.util.spec_from_file_location("task_driver_under_test", DRIVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


driver = load_driver()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def git(repo: Path, *argv: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {' '.join(argv)}: {proc.stderr}"
    return proc.stdout


def make_repo(path: Path, *, branch: str = "main") -> Path:
    """A real one-commit repo: lanes branch from a committed base."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", branch)
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "lane test")
    (path / "README.md").write_text("base\n")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "base")
    return path


def write_config(tmp_path: Path, project_id: str, **overrides) -> Path:
    cfg = {
        "enabled": True,
        "root": str(tmp_path / "lanes"),
        "base_branch": "main",
        "count": 2,
        "setup": [],
    }
    cfg.update(overrides)
    path = tmp_path / "worktrees.json"
    path.write_text(json.dumps({project_id: cfg}))
    return path


# ---------------------------------------------------------------------------
# Unit: identity, config
# ---------------------------------------------------------------------------


def test_lane_branch_is_the_card_id():
    assert driver.lane_branch("SP-047") == "task/SP-047"
    assert driver.lane_branch("T-052") == "task/T-052"


def test_missing_config_means_shared_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "WORKTREES_JSON", tmp_path / "absent.json")
    assert driver.load_lane_config("salon-platform") is None


def test_disabled_or_corrupt_config_never_enables_lanes(tmp_path, monkeypatch):
    disabled = write_config(tmp_path, "p", enabled=False)
    monkeypatch.setattr(driver, "WORKTREES_JSON", disabled)
    assert driver.load_lane_config("p") is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    monkeypatch.setattr(driver, "WORKTREES_JSON", corrupt)
    assert driver.load_lane_config("p") is None

    # a project the file does not mention keeps the shared tree
    other = write_config(tmp_path, "p")
    monkeypatch.setattr(driver, "WORKTREES_JSON", other)
    assert driver.load_lane_config("someone-else") is None


def test_config_is_read_per_project(tmp_path, monkeypatch):
    path = write_config(tmp_path, "salon-platform", count=3)
    monkeypatch.setattr(driver, "WORKTREES_JSON", path)
    cfg = driver.load_lane_config("salon-platform")
    assert cfg is not None and cfg["count"] == 3


# ---------------------------------------------------------------------------
# Unit: allocation, resume, verifier, exhaustion
# ---------------------------------------------------------------------------


def test_allocation_creates_branch_and_lane_dir(tmp_path):
    repo = make_repo(tmp_path / "repo")
    cfg = {"root": str(tmp_path / "lanes"), "base_branch": "main", "count": 2}

    lane, branch, created = driver.resolve_lane(repo, "T-001", cfg, create=True)

    assert created is True
    assert branch == "task/T-001"
    assert lane == tmp_path / "lanes" / "1"
    assert lane.exists() and (lane / "README.md").read_text() == "base\n"
    assert git(repo, "branch", "--list", "task/T-001").strip() != ""


def test_second_card_gets_a_second_lane(tmp_path):
    repo = make_repo(tmp_path / "repo")
    cfg = {"root": str(tmp_path / "lanes"), "base_branch": "main", "count": 2}

    first, _, _ = driver.resolve_lane(repo, "T-001", cfg, create=True)
    second, _, created = driver.resolve_lane(repo, "T-002", cfg, create=True)

    assert created is True
    assert first != second
    assert {first.name, second.name} == {"1", "2"}


def test_redispatch_resumes_the_existing_lane(tmp_path):
    """Budget breach / dead agent / verifier FAIL: the fresh driver must pick
    up the SAME tree, not a clean one."""
    repo = make_repo(tmp_path / "repo")
    cfg = {"root": str(tmp_path / "lanes"), "base_branch": "main", "count": 2}

    lane, _, _ = driver.resolve_lane(repo, "T-001", cfg, create=True)
    (lane / "work-in-progress.txt").write_text("partial attempt\n")

    again, branch, created = driver.resolve_lane(repo, "T-001", cfg, create=True)

    assert created is False
    assert again == lane
    assert branch == "task/T-001"
    assert (again / "work-in-progress.txt").exists()
    # no second lane was consumed by the retry
    assert not (tmp_path / "lanes" / "2").exists()


def test_verifier_never_creates_a_lane(tmp_path):
    """A verifier must not be the thing that produces the tree it grades."""
    repo = make_repo(tmp_path / "repo")
    cfg = {"root": str(tmp_path / "lanes"), "base_branch": "main", "count": 2}

    with pytest.raises(driver.LaneError):
        driver.resolve_lane(repo, "T-001", cfg, create=False)
    assert not (tmp_path / "lanes").exists()

    # and once the implementer created it, the verifier resolves the same path
    lane, _, _ = driver.resolve_lane(repo, "T-001", cfg, create=True)
    resolved, branch, created = driver.resolve_lane(repo, "T-001", cfg, create=False)
    assert (resolved, branch, created) == (lane, "task/T-001", False)


def test_no_free_lane_names_the_pool(tmp_path):
    repo = make_repo(tmp_path / "repo")
    cfg = {"root": str(tmp_path / "lanes"), "base_branch": "main", "count": 1}
    driver.resolve_lane(repo, "T-001", cfg, create=True)

    with pytest.raises(driver.LaneError) as err:
        driver.resolve_lane(repo, "T-002", cfg, create=True)
    message = str(err.value)
    assert "no free lane" in message
    assert "count=1" in message


def test_concurrent_allocation_never_shares_a_lane(tmp_path):
    """Two dispatches in the same second: the flock must serialise them —
    `test -d` style probing would hand both callers lane 1."""
    repo = make_repo(tmp_path / "repo")
    cfg = {"root": str(tmp_path / "lanes"), "base_branch": "main", "count": 2}
    script = tmp_path / "alloc.py"
    script.write_text(
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('d', {str(DRIVER)!r})\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "cfg = json.loads(sys.argv[2])\n"
        "lane, branch, created = mod.resolve_lane("
        f"__import__('pathlib').Path({str(repo)!r}), sys.argv[1], cfg, create=True)\n"
        "print(lane)\n"
    )
    env = {**os.environ, "KANBAN_WORKTREES_JSON": str(tmp_path / "none.json")}
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), task, json.dumps(cfg)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for task in ("T-001", "T-002")
    ]
    out = [p.communicate(timeout=60) for p in procs]

    for p, (stdout, stderr) in zip(procs, out):
        assert p.returncode == 0, f"allocation failed: {stderr}"
    lanes = {stdout.strip() for stdout, _ in out}
    assert len(lanes) == 2, f"both allocators got the same lane: {lanes}"
    assert git(repo, "branch", "--list", "task/T-001").strip() != ""
    assert git(repo, "branch", "--list", "task/T-002").strip() != ""


# ---------------------------------------------------------------------------
# Unit: lane bootstrap
# ---------------------------------------------------------------------------


def test_setup_runs_in_the_lane_and_failure_aborts(tmp_path):
    lane = tmp_path / "lane"
    lane.mkdir()
    log: list[str] = []

    driver.run_lane_setup(lane, {"setup": ["echo ok > marker.txt"]}, log.append)
    assert (lane / "marker.txt").read_text().strip() == "ok"

    with pytest.raises(driver.LaneError) as err:
        driver.run_lane_setup(lane, {"setup": ["exit 3"]}, log.append)
    assert "lane setup failed" in str(err.value)
    # the failing command's output is preserved for the dispatch log
    assert any("exit=3" in line for line in log)


def test_board_row_config_wins_over_the_file(tmp_path, monkeypatch):
    """The board row is the primary source (one fetch, PATCH-able); the
    gitignored file is the fallback for a board whose API predates it."""
    monkeypatch.setattr(
        driver, "WORKTREES_JSON", write_config(tmp_path, "p", count=7)
    )
    from_file = driver.lane_config("p", {})
    assert from_file is not None and from_file["count"] == 7

    from_row = driver.lane_config("p", {"worktrees": {"enabled": True, "count": 2}})
    assert from_row == {"enabled": True, "count": 2}

    # an explicit off-switch on the row is authoritative: a stale file entry
    # must not resurrect lane mode
    assert driver.lane_config("p", {"worktrees": {"enabled": False}}) is None

    # a project the row says nothing about still falls back to the file
    assert driver.lane_config("p", {"worktrees": None}) is not None


# ---------------------------------------------------------------------------
# End to end: the real driver against a fake board + fake omp
# ---------------------------------------------------------------------------

FAKE_OMP = """#!/usr/bin/env python3
import json, os, sys

pf = os.environ.get("FAKE_PROMPT_FILE")
sys.stdout.write(json.dumps({"type": "ready", "protocolVersion": 1}) + "\\n")
sys.stdout.flush()
while True:
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    try:
        obj = json.loads(line)
    except ValueError:
        continue
    if obj.get("type") == "prompt":
        if pf:
            open(pf, "w").write(obj.get("message", ""))
        sys.stdout.write(json.dumps({
            "type": "agent_end", "messages": [], "isTerminal": True}) + "\\n")
        sys.stdout.flush()
"""


class FakeBoard:
    """The driver's endpoints, recording comments/moves/run rows.

    ``status_after_register`` simulates the agent finishing: the moment the
    run row is registered, the card reports the status the driver waits to
    see (work: testing, verify: done) so the session exits on its own.
    """

    def __init__(self, project_path: Path, task_status: str, status_after_register: str):
        self.project_path = project_path
        self.task_status = task_status
        self.status_after_register = status_after_register
        self.comments: list[str] = []
        self.moves: list[str] = []
        self.claims = 0
        self.run: dict = {}
        self.worktrees_field = None

    def serve(self) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        import threading

        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._server = server
        return f"http://127.0.0.1:{server.server_port}"

    def close(self) -> None:
        self._server.shutdown()


def _handler(board: FakeBoard):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _task(self) -> dict:
            return {
                "id": "T-LANE",
                "title": "lane test",
                "status": board.task_status,
                "assignee": "agent:default",
                "kind": "task",
                "parent_id": None,
                "ancestors": [],
                "history": [],
            }

        def _read(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else {}

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path.startswith("/api/board"):
                self._send(
                    200,
                    {
                        "project": {
                            "id": "agent-kanban",
                            "path": str(board.project_path),
                            "constraints": [],
                            "worktrees": board.worktrees_field,
                        },
                        "tasks": {},
                    },
                )
            elif "since_seq" in self.path:
                self._send(200, {"history": []})
            elif self.path.endswith("/context"):
                self._send(
                    200,
                    {
                        "task_id": "T-LANE",
                        "task": self._task(),
                        "ancestors": [],
                        "comments": [],
                        "constraints": ["constraint one"],
                    },
                )
            elif self.path.endswith("/runs"):
                self._send(200, {"run": board.run})
            elif "/api/tasks/" in self.path:
                self._send(200, self._task())
            else:
                self._send(404, {"detail": "no"})

        def do_POST(self) -> None:
            if "/claim" in self.path:
                board.claims += 1
                self._send(200, self._task())
            elif "/comment" in self.path:
                board.comments.append(self._read().get("text", ""))
                self._send(200, {"ok": True})
            elif "/move" in self.path:
                to_status = self._read().get("to_status", "")
                board.moves.append(to_status)
                board.task_status = to_status or board.task_status
                self._send(200, self._task())
            elif "/runs" in self.path:
                body = self._read()
                board.run = {**board.run, **{k: v for k, v in body.items() if v}}
                if body.get("status") == "running":
                    board.task_status = board.status_after_register
                self._send(200, {"run": board.run})
            elif "/chat" in self.path:
                self._read()
                self._send(201, {"seq": 1})
            else:
                self._send(404, {"detail": "no"})

    return H


def run_driver(board_url: str, tmp_path: Path, *extra: str, config: Path | None = None):
    fake = tmp_path / "fake_omp.py"
    fake.write_text(FAKE_OMP)
    fake.chmod(0o755)
    prompt_file = tmp_path / "prompt.txt"
    env = {
        **os.environ,
        "OMP_BIN": str(fake),
        "FAKE_PROMPT_FILE": str(prompt_file),
        "KANBAN_WORKTREES_JSON": str(config or (tmp_path / "absent.json")),
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(DRIVER),
            "--task-id", "T-LANE",
            "--project-id", "agent-kanban",
            "--base-url", board_url,
            *extra,
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=90,
        check=False,
    )
    return proc, prompt_file


@pytest.fixture()
def lane_env(tmp_path):
    repo = make_repo(tmp_path / "repo")
    config = write_config(tmp_path, "agent-kanban", root=str(tmp_path / "lanes"))
    yield tmp_path, repo, config
    (AGENT_LOG_DIR / "T-LANE.log").unlink(missing_ok=True)


def test_work_dispatch_runs_in_the_lane_and_records_it(lane_env):
    tmp_path, repo, config = lane_env
    board = FakeBoard(repo, task_status="approved", status_after_register="testing")
    url = board.serve()
    try:
        proc, prompt_file = run_driver(url, tmp_path, config=config)
    finally:
        board.close()

    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    lane = tmp_path / "lanes" / "1"
    assert lane.exists(), "work dispatch did not create a lane"
    assert git(repo, "branch", "--list", "task/T-LANE").strip() != ""
    assert board.claims == 1
    # the run row carries the lane so a verifier can find the same tree
    assert board.run["worktree"] == str(lane)
    # the agent is told where it is and what it may not touch
    prompt = prompt_file.read_text()
    assert "task/T-LANE" in prompt
    assert str(lane) in prompt
    assert "never create, remove or prune worktrees" in prompt


def test_no_config_keeps_the_shared_project_tree(lane_env):
    """The compatibility guarantee: without lane config the driver runs in
    the project tree and creates nothing."""
    tmp_path, repo, _config = lane_env
    board = FakeBoard(repo, task_status="approved", status_after_register="testing")
    url = board.serve()
    try:
        proc, prompt_file = run_driver(url, tmp_path)  # no config
    finally:
        board.close()

    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    assert not (tmp_path / "lanes").exists()
    assert git(repo, "branch", "--list", "task/T-LANE").strip() == ""
    assert board.run["worktree"] == str(repo)
    prompt = prompt_file.read_text()
    assert "task/T-LANE" not in prompt
    assert "never create, remove or prune worktrees" not in prompt


def test_verify_resolves_the_implementers_lane(lane_env):
    """The verifier grades the lane the implementer wrote — never master."""
    tmp_path, repo, config = lane_env
    driver.resolve_lane(
        repo,
        "T-LANE",
        {"root": str(tmp_path / "lanes"), "base_branch": "main", "count": 2},
        create=True,
    )
    board = FakeBoard(repo, task_status="testing", status_after_register="done")
    url = board.serve()
    try:
        proc, prompt_file = run_driver(url, tmp_path, "--mode", "verify", config=config)
    finally:
        board.close()

    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    assert board.claims == 0, "a verifier must not claim the card"
    assert board.run["worktree"] == str(tmp_path / "lanes" / "1")
    assert "task/T-LANE" in prompt_file.read_text()


def test_verify_without_a_lane_does_not_create_one(lane_env):
    """Lane config on, but the card never ran in a lane (shared-tree run, or
    config added later): the verifier falls back to the project tree instead
    of minting a lane — otherwise the "work" it grades would be its own."""
    tmp_path, repo, config = lane_env
    board = FakeBoard(repo, task_status="testing", status_after_register="done")
    url = board.serve()
    try:
        proc, prompt_file = run_driver(url, tmp_path, "--mode", "verify", config=config)
    finally:
        board.close()

    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    assert not (tmp_path / "lanes").exists(), "verifier created a lane"
    assert board.run["worktree"] == str(repo)
    assert "never create, remove or prune worktrees" not in prompt_file.read_text()


def test_no_free_lane_aborts_before_claim(lane_env):
    """A lane-declared card must never fall back to the shared tree."""
    tmp_path, repo, _config = lane_env
    config = write_config(
        tmp_path, "agent-kanban", root=str(tmp_path / "lanes"), count=1
    )
    lane, _, _ = driver.resolve_lane(
        repo,
        "T-OTHER",
        {"root": str(tmp_path / "lanes"), "base_branch": "main", "count": 1},
        create=True,
    )
    assert lane.exists()
    board = FakeBoard(repo, task_status="approved", status_after_register="testing")
    url = board.serve()
    try:
        proc, _prompt = run_driver(url, tmp_path, config=config)
    finally:
        board.close()

    assert proc.returncode == 5, f"expected the lane-abort exit:\n{proc.stdout}"
    assert board.claims == 0, "aborted dispatch must not claim the card"
    assert any("no free lane" in c for c in board.comments), board.comments
    # only the pre-existing lane exists — nothing new was minted
    assert sorted(p.name for p in (tmp_path / "lanes").iterdir() if p.is_dir()) == ["1"]


def test_board_served_config_enables_lanes_without_a_file(lane_env):
    """The lane config can come entirely from the board project row — the
    primary source, so no file has to be deployed next to the driver."""
    tmp_path, repo, _config = lane_env
    board = FakeBoard(repo, task_status="approved", status_after_register="testing")
    board.worktrees_field = {
        "enabled": True,
        "root": str(tmp_path / "board-lanes"),
        "base_branch": "main",
        "count": 2,
        "setup": [],
    }
    url = board.serve()
    try:
        proc, prompt_file = run_driver(url, tmp_path)  # no config file at all
    finally:
        board.close()

    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    lane = tmp_path / "board-lanes" / "1"
    assert lane.exists(), "board-served lane config did not create a lane"
    assert board.run["worktree"] == str(lane)
    assert "task/T-LANE" in prompt_file.read_text()


def test_board_off_switch_survives_a_stale_file_entry(lane_env):
    tmp_path, repo, config = lane_env  # the file has lanes enabled
    board = FakeBoard(repo, task_status="approved", status_after_register="testing")
    board.worktrees_field = {"enabled": False}
    url = board.serve()
    try:
        proc, _prompt = run_driver(url, tmp_path, config=config)
    finally:
        board.close()

    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    assert not (tmp_path / "lanes").exists(), "stale file entry resurrected lane mode"
    assert board.run["worktree"] == str(repo)

"""Lane release: examples/lane-release.sh (merge-then-remove of a card lane).

One git worktree + branch per card (``task/<id>``) is the "worktree lanes"
feature; the branch IS the lane identity and ``git worktree list --porcelain``
is the only ledger. Release runs AFTER the orchestrator merged that branch:
drop the worktree, delete the branch. The interesting contract is the refusal
path — releasing a lane that still holds unmerged commits or uncommitted files
destroys work nobody has seen, so the script must exit 3 and leave the lane
untouched unless ``--force`` says the loss is intended.

Each test builds a real repo and a real lane (no board, no live config: the
env overrides LANE_PROJECT_PATH / LANE_CONFIG are what the orchestrator's own
lane runs use, and KANBAN_URL is pointed at a dead port so a regression that
starts talking to the board cannot touch the live 7777 board).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "examples" / "lane-release.sh"
PROJECT = "lane-release-test"
TASK = "T-001"
BRANCH = f"task/{TASK}"


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def worktree_paths(repo: Path) -> list[Path]:
    """Registered worktrees, canonicalised (git reports /private/var on macOS)."""
    out = git("worktree", "list", "--porcelain", cwd=repo).stdout
    return [
        Path(os.path.realpath(line[len("worktree ") :]))
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def branches(repo: Path) -> list[str]:
    out = git("branch", "--list", "--format=%(refname:short)", cwd=repo).stdout
    return out.split()


@dataclass
class Lane:
    repo: Path
    path: Path
    base: str
    env: dict[str, str]

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=self.env,
        )

    def has_lane(self) -> bool:
        return Path(os.path.realpath(self.path)) in worktree_paths(self.repo)


@pytest.fixture()
def lane(tmp_path: Path) -> Lane:
    """A real repo on ``main`` with a lane worktree on task/T-001 checked out."""
    repo = tmp_path / "proj"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "Test", cwd=repo)
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    git("add", "file.txt", cwd=repo)
    git("commit", "-qm", "init", cwd=repo)
    base = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout.strip()

    path = tmp_path / "lanes" / "1"
    path.parent.mkdir()
    git("worktree", "add", "-q", "-b", BRANCH, str(path), base, cwd=repo)

    config = tmp_path / "worktrees.json"
    config.write_text(
        json.dumps(
            {
                PROJECT: {
                    "enabled": True,
                    "root": str(tmp_path / "lanes"),
                    "base_branch": base,
                    "count": 1,
                    "setup": [],
                }
            }
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "LANE_PROJECT_PATH": str(repo),
        "LANE_CONFIG": str(config),
        # dead port: a regression that fetches the board must fail loudly here
        # instead of reading (or disturbing) the live board on 7777.
        "KANBAN_URL": "http://127.0.0.1:1",
    }
    return Lane(repo=repo, path=path, base=base, env=env)


def _rows(proc: subprocess.CompletedProcess[str]) -> list[dict[str, str]]:
    """Parse the ``lane key=value ...`` rows of ``--check``."""
    rows = []
    for line in proc.stdout.splitlines():
        if line.startswith("lane "):
            rows.append(dict(kv.split("=", 1) for kv in line.split()[1:]))
    return rows


def test_check_lists_lane_clean_and_merged(lane: Lane) -> None:
    """--check reports branch, dirty and unmerged without touching the lane."""
    proc = lane.run(PROJECT, "--check")

    assert proc.returncode == 0, proc.stderr
    rows = _rows(proc)
    assert len(rows) == 1, proc.stdout
    row = rows[0]
    assert row["branch"] == BRANCH
    assert row["task"] == TASK
    assert row["exists"] == "true"
    assert row["dirty"] == "false"
    assert row["unmerged"] == "false"
    assert lane.has_lane()
    assert BRANCH in branches(lane.repo)


def test_check_reports_dirty_lane(lane: Lane) -> None:
    """An uncommitted file in the lane is visible in --check (untracked too)."""
    (lane.path / "scratch.txt").write_text("wip\n", encoding="utf-8")

    proc = lane.run(PROJECT, "--check")

    assert proc.returncode == 0, proc.stderr
    assert _rows(proc)[0]["dirty"] == "true"


def test_release_removes_worktree_and_branch(lane: Lane) -> None:
    """The happy path: merged + clean -> worktree and branch both gone."""
    proc = lane.run(PROJECT, TASK)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert BRANCH in proc.stdout
    assert not lane.has_lane()
    assert BRANCH not in branches(lane.repo)
    assert not lane.path.exists()


def test_release_refuses_dirty_lane(lane: Lane) -> None:
    """Uncommitted work in the lane is not silently discarded (exit 3)."""
    (lane.path / "scratch.txt").write_text("wip\n", encoding="utf-8")

    proc = lane.run(PROJECT, TASK)

    assert proc.returncode == 3
    assert "dirty" in (proc.stdout + proc.stderr).lower()
    assert lane.has_lane()
    assert BRANCH in branches(lane.repo)


def test_release_refuses_unmerged_lane(lane: Lane) -> None:
    """Commits the base branch never saw block release (exit 3)."""
    (lane.path / "file.txt").write_text("lane change\n", encoding="utf-8")
    git("commit", "-qam", "lane work", cwd=lane.path)

    proc = lane.run(PROJECT, TASK)

    assert proc.returncode == 3
    assert "unmerged" in (proc.stdout + proc.stderr).lower()
    assert lane.has_lane()
    assert BRANCH in branches(lane.repo)


def test_force_releases_dirty_unmerged_lane(lane: Lane) -> None:
    """--force means the data loss is intended: dirty AND unmerged still go."""
    (lane.path / "scratch.txt").write_text("wip\n", encoding="utf-8")
    (lane.path / "file.txt").write_text("lane change\n", encoding="utf-8")
    git("commit", "-qam", "lane work", cwd=lane.path)
    (lane.path / "scratch2.txt").write_text("wip\n", encoding="utf-8")

    proc = lane.run(PROJECT, TASK, "--force")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not lane.has_lane()
    assert BRANCH not in branches(lane.repo)


def test_unknown_task_exits_4(lane: Lane) -> None:
    """No worktree on that branch is a distinct outcome (exit 4), not a refusal."""
    proc = lane.run(PROJECT, "T-999")

    assert proc.returncode == 4
    assert "T-999" in proc.stdout + proc.stderr
    assert lane.has_lane()

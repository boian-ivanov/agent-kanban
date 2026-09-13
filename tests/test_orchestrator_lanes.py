"""The lane close step: merge a card's branch into the base branch.

Why this test exists — a live failure on SP-048 (2026-09-13): `git merge
--no-ff task/SP-048` produced git's default merge message ("Merge branch
'task/SP-048'"), salon-platform's lefthook commit-msg hook rejected it, and
the merge was left uncommitted with MERGE_HEAD set — the card's work was
committed in the lane but landed nowhere, and the close reported failure with
"no unmerged paths listed" (nothing was actually in conflict).

The regression this pins: `merge_lane` must produce a merge commit whose
message satisfies a `type(scope): …` commit-msg hook, so the close finishes in
one pass on a repo that enforces one.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR = REPO_ROOT / "examples" / "orchestrator.py"

# The salon-platform commit-msg rule (its lefthook.yml), narrowed to what the
# merge commit has to satisfy.
HOOK = """#!/bin/sh
grep -qE '^(feat|fix|refactor|test|perf)\\([A-Z]{1,4}-[0-9]{3}(,[A-Z]{1,4}-[0-9]{3})*\\): .+|^(chore|docs|style|ci|build|revert)(\\([A-Z]{1,4}-[0-9]{3}\\))?: .+' "$1"
"""


def load_orchestrator():
    spec = importlib.util.spec_from_file_location("orchestrator_under_test", ORCHESTRATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # A dataclass resolves its own module through sys.modules during class
    # creation — without this registration the import dies in dataclasses.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


orch = load_orchestrator()


def git(repo: Path, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True, check=False
    )
    if check:
        assert proc.returncode == 0, f"git {' '.join(argv)}: {proc.stderr}"
    return proc


def make_repo_with_hook(path: Path) -> Path:
    """A repo whose commit-msg hook enforces salon-platform's convention."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "master")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "lane close test")
    (path / "README.md").write_text("base\n")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "chore: base")
    hook = path / ".git" / "hooks" / "commit-msg"
    hook.write_text(HOOK)
    hook.chmod(0o755)
    return path


@pytest.fixture()
def lane(tmp_path):
    repo = make_repo_with_hook(tmp_path / "repo")
    lane_dir = tmp_path / "lanes" / "1"
    git(repo, "worktree", "add", "-b", "task/T-001", str(lane_dir), "master")
    (lane_dir / "work.txt").write_text("card work\n")
    git(lane_dir, "add", "-A")
    git(lane_dir, "commit", "-m", "feat(T-001): the card's work")
    return orch.Lane(
        project_id="scratch",
        repo=repo,
        branch="task/T-001",
        base_branch="master",
        lane=lane_dir,
    )


def test_gits_default_merge_message_is_rejected_by_the_hook(lane):
    """The failure mode itself: without a conforming message the merge never
    commits, which is what stranded SP-048 mid-merge."""
    proc = git(lane.repo, "merge", "--no-ff", lane.branch, check=False)
    assert proc.returncode != 0
    assert (lane.repo / ".git" / "MERGE_HEAD").exists()
    git(lane.repo, "merge", "--abort")
    # the lane and its commit survived the abort: the work is never lost
    assert lane.lane is not None and (lane.lane / "work.txt").exists()


def test_merge_lane_lands_a_conventional_merge_commit(lane):
    sha = orch.merge_lane(lane)

    assert sha == git(lane.repo, "rev-parse", "HEAD").stdout.strip()
    subject = git(lane.repo, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "chore(T-001): merge task/T-001 into master"
    # a real merge (two parents), and the card's work is on the base branch
    parents = git(lane.repo, "log", "-1", "--format=%P").stdout.split()
    assert len(parents) == 2
    assert (lane.repo / "work.txt").read_text() == "card work\n"
    assert not (lane.repo / ".git" / "MERGE_HEAD").exists()


def test_merge_lane_refuses_when_base_branch_is_not_checked_out(lane):
    """The merge lands on HEAD — merging from another branch would publish a
    stale ref, so the close refuses instead of guessing."""
    git(lane.repo, "checkout", "-b", "side")
    with pytest.raises(orch.CloseError) as err:
        orch.merge_lane(lane)
    assert "refusing to merge" in str(err.value)
    assert not (lane.repo / ".git" / "MERGE_HEAD").exists()

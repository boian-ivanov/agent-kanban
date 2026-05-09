"""Loose-mode parser tests: verify that sections with non-canonical
headings (`## 🔴 Tier 0`, `## Status snapshot`, `## v2 stages`) land in
backlog with a section_label, while canonical headings (`## Backlog`,
`## Done`) keep the previous behaviour — label=None.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from kanban_store import Store
from kanban_ui.automation.plan_md import (
    ParsedTask,
    import_plan_md,
    parse_plan_md,
)


# ---------- parse_plan_md tests ----------

def test_canonical_headings_no_label():
    text = "## Backlog\n- [ ] A\n## Done\n- [x] B\n"
    result = parse_plan_md(text)
    assert result == [
        ParsedTask(title="A", done=False, status="backlog", section_label=None),
        ParsedTask(title="B", done=True, status="done", section_label=None),
    ]


def test_loose_section_with_emoji_falls_into_backlog():
    text = (
        "## 🔴 Tier 0\n"
        "- [ ] X\n"
        "## 🟡 Tier 0.5\n"
        "- [ ] Y\n"
    )
    result = parse_plan_md(text)
    assert len(result) == 2
    assert result[0] == ParsedTask(
        title="X", done=False, status="backlog", section_label="🔴 Tier 0"
    )
    assert result[1] == ParsedTask(
        title="Y", done=False, status="backlog", section_label="🟡 Tier 0.5"
    )


def test_mixed_canonical_and_loose():
    text = (
        "## Status snapshot\n"
        "- [ ] Z\n"
        "## Done\n"
        "- [x] W\n"
    )
    result = parse_plan_md(text)
    assert result == [
        ParsedTask(title="Z", done=False, status="backlog", section_label="Status snapshot"),
        ParsedTask(title="W", done=True, status="done", section_label=None),
    ]


def test_russian_canonical_and_loose():
    text = (
        "## Бэклог\n"
        "- [ ] A\n"
        "## Произвольная секция\n"
        "- [ ] B\n"
    )
    result = parse_plan_md(text)
    assert result == [
        ParsedTask(title="A", done=False, status="backlog", section_label=None),
        ParsedTask(title="B", done=False, status="backlog", section_label="Произвольная секция"),
    ]


def test_tasks_before_first_heading_ignored():
    text = (
        "- [ ] orphan task before any heading\n"
        "## Backlog\n"
        "- [ ] valid task\n"
    )
    result = parse_plan_md(text)
    assert len(result) == 1
    assert result[0].title == "valid task"


def test_empty_text():
    assert parse_plan_md("") == []


# ---------- import_plan_md tests ----------

@pytest.fixture
def temp_store(tmp_path):
    db_path = tmp_path / "test.db"
    # default project comes from env vars (see _migrate_v2)
    os.environ["KANBAN_DEFAULT_PROJECT_ID"] = "test"
    os.environ["KANBAN_DEFAULT_PROJECT_NAME"] = "Test"
    store = Store(db_path)
    yield store


def test_import_loose_writes_section_label_to_description(temp_store, tmp_path):
    plan_file = tmp_path / "PLAN.md"
    plan_file.write_text(
        "## 🔴 Tier 0\n"
        "- [ ] Critical work\n"
        "## Done\n"
        "- [x] Already done\n",
        encoding="utf-8",
    )
    counts = import_plan_md(temp_store, "test", plan_file)
    assert counts == {"created": 2, "skipped": 0}

    tasks = temp_store.list_tasks(project_id="test")
    by_title = {t.title: t for t in tasks}
    crit = by_title["Critical work"]
    assert crit.status == "backlog"
    assert "Tier 0" in crit.description
    assert "From section" in crit.description

    done = by_title["Already done"]
    assert done.status == "done"
    assert done.description == ""  # canonical → empty description


def test_import_idempotent(temp_store, tmp_path):
    plan_file = tmp_path / "PLAN.md"
    plan_file.write_text(
        "## Backlog\n- [ ] dedup-me\n",
        encoding="utf-8",
    )
    first = import_plan_md(temp_store, "test", plan_file)
    second = import_plan_md(temp_store, "test", plan_file)
    assert first == {"created": 1, "skipped": 0}
    assert second == {"created": 0, "skipped": 1}

"""Tests for journal tool wrappers (mock `subprocess.run`).

Each tool builds an argv list and shells out via `run_command`. These
tests verify argv shape for happy paths, defaults vs custom den_path,
offset handling, and the three error branches in `run_command`.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Dict

import pytest

from utils.tools.journal_tools import (
    DEFAULT_DEN_PATH,
    daily_view_tool,
    habits_toggle_tool,
    macros_entry_tool,
    macros_insert_tool,
    macros_lookup_tool,
    macros_search_tool,
    notes_entry_tool,
    notes_filter_tool,
    tasks_entry_tool,
    tasks_remove_tool,
    tasks_toggle_tool,
    tasks_tomorrow_entry_tool,
    tasks_tomorrow_remove_tool,
    today_create_tool,
    weight_entry_tool,
)

# Package __init__ rebinds the submodule attribute; reach via sys.modules.
journal_mod = sys.modules["utils.tools.journal_tools"]


class _FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def recorder(monkeypatch) -> Dict[str, Any]:
    """Capture argv passed to subprocess.run; return canned stdout."""
    calls: Dict[str, Any] = {"argv": None, "kwargs": None}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return _FakeCompleted(stdout="ok output")

    monkeypatch.setattr(journal_mod.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# today_create_tool
# ---------------------------------------------------------------------------


def test_today_create_default_path_omits_path_arg(recorder):
    today_create_tool.invoke({})
    assert recorder["argv"] == ["today", "--create"]


def test_today_create_default_explicit_value_also_omits_path(recorder):
    today_create_tool.invoke({"den_path": DEFAULT_DEN_PATH})
    assert recorder["argv"] == ["today", "--create"]


def test_today_create_custom_path_is_inserted(recorder):
    today_create_tool.invoke({"den_path": "/tmp/elsewhere"})
    assert recorder["argv"] == ["today", "/tmp/elsewhere", "--create"]


# ---------------------------------------------------------------------------
# daily_view_tool / offset handling
# ---------------------------------------------------------------------------


def test_daily_view_default(recorder):
    daily_view_tool.invoke({})
    assert recorder["argv"] == ["daily"]


def test_daily_view_offset_appends_offset_args(recorder):
    daily_view_tool.invoke({"offset": -1})
    assert recorder["argv"] == ["daily", "--offset", "-1"]


def test_daily_view_zero_offset_omits_offset_args(recorder):
    daily_view_tool.invoke({"offset": 0})
    assert "--offset" not in recorder["argv"]


# ---------------------------------------------------------------------------
# Macros family
# ---------------------------------------------------------------------------


def test_macros_entry_default(recorder):
    macros_entry_tool.invoke({"text": "egg 2pc,12,0,10"})
    assert recorder["argv"] == ["daily", "--macros-entry", "egg 2pc,12,0,10"]


def test_macros_entry_with_offset(recorder):
    macros_entry_tool.invoke({"text": "egg 2pc,12,0,10", "offset": 1})
    assert recorder["argv"] == [
        "daily",
        "--macros-entry",
        "egg 2pc,12,0,10",
        "--offset",
        "1",
    ]


def test_macros_lookup_passes_query_as_positional(recorder):
    macros_lookup_tool.invoke({"food_query": "chicken breast 100g"})
    assert recorder["argv"] == ["macros", "chicken breast 100g"]


def test_macros_search_uses_q_flag(recorder):
    macros_search_tool.invoke({"search_query": "chick"})
    assert recorder["argv"] == ["macros", "-q", "chick"]


def test_macros_insert_uses_i_flag(recorder):
    macros_insert_tool.invoke({"food_entry": "banana 100g,1,23,0.3"})
    assert recorder["argv"] == ["macros", "-i", "banana 100g,1,23,0.3"]


# ---------------------------------------------------------------------------
# Notes / habits / tasks / weight smoke tests
# ---------------------------------------------------------------------------


def test_notes_entry(recorder):
    notes_entry_tool.invoke({"text": "did a thing"})
    assert recorder["argv"] == ["daily", "--notes-entry", "did a thing"]


def test_notes_filter_by_tag(recorder):
    notes_filter_tool.invoke({"tag": "workout"})
    assert recorder["argv"] == ["daily", "--note", "workout"]


def test_habits_toggle(recorder):
    habits_toggle_tool.invoke({"habit_name": "Gym"})
    assert recorder["argv"] == ["daily", "--toggle-habit", "Gym"]


def test_tasks_entry(recorder):
    tasks_entry_tool.invoke({"task_text": "Review PR"})
    assert recorder["argv"] == ["daily", "--task-entry", "Review PR"]


def test_tasks_tomorrow_entry(recorder):
    tasks_tomorrow_entry_tool.invoke({"task_text": "Buy milk"})
    assert recorder["argv"] == ["daily", "--task-tomorrow-entry", "Buy milk"]


def test_tasks_toggle_stringifies_index(recorder):
    tasks_toggle_tool.invoke({"task_index": 2})
    assert recorder["argv"] == ["daily", "--toggle-task", "2"]


def test_tasks_remove_stringifies_index(recorder):
    tasks_remove_tool.invoke({"task_index": 3})
    assert recorder["argv"] == ["daily", "--task-remove", "3"]


def test_tasks_tomorrow_remove_stringifies_index(recorder):
    tasks_tomorrow_remove_tool.invoke({"task_index": 1})
    assert recorder["argv"] == ["daily", "--task-tomorrow-remove", "1"]


def test_weight_entry(recorder):
    weight_entry_tool.invoke({"weight_value": "75"})
    assert recorder["argv"] == ["daily", "--weight-entry", "75"]


# ---------------------------------------------------------------------------
# run_command error branches
# ---------------------------------------------------------------------------


def test_called_process_error_returns_error_message(monkeypatch):
    def boom(argv, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=argv, stderr="thing exploded"
        )

    monkeypatch.setattr(journal_mod.subprocess, "run", boom)
    out = today_create_tool.invoke({})
    assert "Error" in out
    assert "thing exploded" in out


def test_generic_exception_returns_unexpected_error_message(monkeypatch):
    def boom(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(journal_mod.subprocess, "run", boom)
    out = today_create_tool.invoke({})
    assert out.startswith("Unexpected error")
    assert "no such file" in out


def test_empty_stdout_returns_success_marker(monkeypatch):
    def fake_run(argv, **kwargs):
        return _FakeCompleted(stdout="")

    monkeypatch.setattr(journal_mod.subprocess, "run", fake_run)
    out = today_create_tool.invoke({})
    assert out.startswith("✓")

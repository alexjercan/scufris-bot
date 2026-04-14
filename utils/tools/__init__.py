"""Tools for the Scufris Bot agent."""

from .calculator import calculator_tool
from .datetime_tool import datetime_tool
from .journal_tools import (
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
from .opencode_tool import opencode_tool
from .weather_tool import weather_tool
from .web_search import web_search_tool

__all__ = [
    "calculator_tool",
    "daily_view_tool",
    "datetime_tool",
    "habits_toggle_tool",
    "macros_entry_tool",
    "macros_insert_tool",
    "macros_lookup_tool",
    "macros_search_tool",
    "notes_entry_tool",
    "notes_filter_tool",
    "opencode_tool",
    "tasks_entry_tool",
    "tasks_remove_tool",
    "tasks_toggle_tool",
    "tasks_tomorrow_entry_tool",
    "tasks_tomorrow_remove_tool",
    "today_create_tool",
    "weather_tool",
    "web_search_tool",
    "weight_entry_tool",
]

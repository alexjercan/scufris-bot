"""Tools for the Scufris Bot agent."""

from .calculator import calculator_tool
from .datetime_tool import datetime_tool
from .opencode_tool import opencode_tool
from .weather_tool import weather_tool
from .web_search import web_search_tool

__all__ = [
    "calculator_tool",
    "datetime_tool",
    "opencode_tool",
    "weather_tool",
    "web_search_tool",
]

"""Datetime tool for the agent."""

import logging
from datetime import datetime, timezone

from langchain.tools import tool

logger = logging.getLogger("scufris-bot.tools.datetime")


@tool
def datetime_tool(format: str = "%Y-%m-%d %H:%M:%S") -> str:
    """
    Get the current date and time.

    Returns the current date and time in the specified format.
    Default format is "YYYY-MM-DD HH:MM:SS".

    Args:
        format: The strftime format string for the output (default: "%Y-%m-%d %H:%M:%S")

    Returns:
        The current date and time as a formatted string

    Examples:
        >>> datetime_tool()
        "2026-04-06 10:30:45"
        >>> datetime_tool("%A, %B %d, %Y")
        "Monday, April 06, 2026"
    """
    try:
        now = datetime.now(timezone.utc)
        result = now.strftime(format)
        return result
    except Exception as e:
        error_msg = f"Error formatting datetime: {str(e)}"
        logger.error(error_msg)
        return error_msg

"""Journal management tools for the daily journal agent."""

import logging
import subprocess
from typing import Optional

from langchain.tools import tool

logger = logging.getLogger("scufris-bot.tools.journal")

DEFAULT_DEN_PATH = "/home/alex/personal/the-den"


def run_command(command: list[str], description: str) -> str:
    """
    Run a shell command and return the output.

    Args:
        command: List of command arguments
        description: Description of the command for logging

    Returns:
        The command output or error message
    """
    try:
        logger.debug(f"Running command: {' '.join(command)}")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
        )
        output = result.stdout.strip()
        if result.stderr:
            logger.warning(f"Command stderr: {result.stderr}")
        logger.debug(f"{description} completed successfully")
        return output if output else f"✓ {description} completed successfully"
    except subprocess.CalledProcessError as e:
        error_msg = f"Error {description}: {e.stderr if e.stderr else str(e)}"
        logger.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"Unexpected error {description}: {str(e)}"
        logger.error(error_msg)
        return error_msg


@tool
def today_create_tool(den_path: Optional[str] = None) -> str:
    """
    Create today's journal entry if it doesn't exist yet.

    This command creates the daily journal entry for today without opening
    an editor. It's useful for ensuring the entry exists before adding
    content to it.

    Args:
        den_path: Optional path to the den directory (defaults to /home/alex/personal/the-den)

    Returns:
        Success message or error information

    Examples:
        >>> today_create_tool()
        "✓ Created today's journal entry"
        >>> today_create_tool("/path/to/custom/den")
        "✓ Created today's journal entry"
    """
    # Note: The 'today' command uses its own default path, so we only pass
    # the path if it's explicitly provided and different from default
    if den_path and den_path != DEFAULT_DEN_PATH:
        command = ["today", den_path, "--create"]
    else:
        command = ["today", "--create"]
    return run_command(command, "creating today's journal entry")


@tool
def macros_entry_tool(
    text: str, den_path: Optional[str] = None, offset: int = 0
) -> str:
    """
    Add a macros entry to the daily journal.

    This adds text (which can be multi-line) to the '### 🍽️ Macros' section
    of the current daily journal entry. Use this after computing macros
    for a food item.

    Args:
        text: The text to add to the Macros section (e.g., "chicken breast 100g,31,0,4")
        den_path: Optional path to the den directory (defaults to /home/alex/personal/the-den)
        offset: Number of days to offset from today (default: 0)

    Returns:
        Success message or error information

    Examples:
        >>> macros_entry_tool("chicken breast 100g,31,0,4")
        "✓ Added entry to Macros section"
        >>> macros_entry_tool("egg 2pc,12,0,10", offset=1)
        "✓ Added entry to Macros section"
    """
    # Note: The 'daily' command uses its own default path
    if den_path and den_path != DEFAULT_DEN_PATH:
        command = ["daily", den_path, "--macros-entry", text]
    else:
        command = ["daily", "--macros-entry", text]

    if offset != 0:
        command.extend(["--offset", str(offset)])
    return run_command(command, "adding macros entry")


@tool
def notes_entry_tool(text: str, den_path: Optional[str] = None, offset: int = 0) -> str:
    """
    Add a notes entry to the daily journal.

    This adds text (which can be multi-line) to the '### 📝 Notes' section
    of the current daily journal entry.

    Args:
        text: The text to add to the Notes section
        den_path: Optional path to the den directory (defaults to /home/alex/personal/the-den)
        offset: Number of days to offset from today (default: 0)

    Returns:
        Success message or error information

    Examples:
        >>> notes_entry_tool("Had a productive meeting with the team")
        "✓ Added entry to Notes section"
        >>> notes_entry_tool("Reminder: check email", offset=1)
        "✓ Added entry to Notes section"
    """
    # Note: The 'daily' command uses its own default path
    if den_path and den_path != DEFAULT_DEN_PATH:
        command = ["daily", den_path, "--notes-entry", text]
    else:
        command = ["daily", "--notes-entry", text]

    if offset != 0:
        command.extend(["--offset", str(offset)])
    return run_command(command, "adding notes entry")


@tool
def macros_lookup_tool(food_query: str) -> str:
    """
    Look up nutritional macros for a food item.

    This computes the macros (protein, carbs, fat) for a given food item.
    The food query should be in the format: "<name> <qty><unit>"
    (e.g., "chicken breast 100g", "egg 2pc").

    Args:
        food_query: The food item to look up (e.g., "chicken breast 100g", "egg 2pc")

    Returns:
        The macros in format: "<food> <amount><unit>,<protein>,<carbs>,<fat>"
        or an error message if the food is not found

    Examples:
        >>> macros_lookup_tool("chicken breast 100g")
        "chicken breast 100g,31,0,4"
        >>> macros_lookup_tool("egg 2pc")
        "egg 2pc,12,0,10"
    """
    command = ["macros", food_query]
    return run_command(command, f"looking up macros for '{food_query}'")


@tool
def daily_view_tool(den_path: Optional[str] = None, offset: int = 0) -> str:
    """
    View today's journal entry with a compact summary.

    This outputs a compact view of today's journal entry, including
    details about food, tasks, and other tracked information.

    Args:
        den_path: Optional path to the den directory (defaults to /home/alex/personal/the-den)
        offset: Number of days to offset from today (default: 0, use negative for past days)

    Returns:
        A compact summary of the journal entry

    Examples:
        >>> daily_view_tool()
        "📅 2026-04-08\\n\\n### 🍽️ Macros\\n..."
        >>> daily_view_tool(offset=-1)
        "📅 2026-04-07\\n\\n### 🍽️ Macros\\n..."
    """
    # Note: The 'daily' command uses its own default path
    if den_path and den_path != DEFAULT_DEN_PATH:
        command = ["daily", den_path]
    else:
        command = ["daily"]

    if offset != 0:
        command.extend(["--offset", str(offset)])
    return run_command(command, "viewing daily journal")


@tool
def macros_search_tool(search_query: str) -> str:
    """
    Search for foods in the macros database using fuzzy matching.

    This performs a fuzzy search to find foods that match the search query.
    Useful when you don't know the exact name of a food item or want to
    see what similar foods are available in the database.

    Args:
        search_query: The search term to find matching foods (e.g., "chick", "egg")

    Returns:
        A list of matching foods or a message if no matches found

    Examples:
        >>> macros_search_tool("chick")
        "Foods matching 'chick':\\n  chicken breast g\\n  chicken thigh g\\n..."
        >>> macros_search_tool("egg")
        "Foods matching 'egg':\\n  egg pc\\n  veg mix medi g\\n..."
    """
    command = ["macros", "-q", search_query]
    return run_command(command, f"searching for foods matching '{search_query}'")


@tool
def macros_insert_tool(food_entry: str) -> str:
    """
    Add a new food item to the macros database.

    This inserts a new food entry into the database. The entry must be in the
    format: "<food> <amount><unit>,<protein>,<carbs>,<fat>"

    Args:
        food_entry: The food entry to add in format "food amount unit,protein,carbs,fat"
                   (e.g., "banana 100g,1,23,0.3")

    Returns:
        Success message or error information

    Examples:
        >>> macros_insert_tool("banana 100g,1,23,0.3")
        "✓ Added 'banana 100g' to macros database"
        >>> macros_insert_tool("custom recipe 100g,30,40,10")
        "✓ Added 'custom recipe 100g' to macros database"
    """
    command = ["macros", "-i", food_entry]
    return run_command(command, f"adding '{food_entry}' to macros database")


@tool
def notes_filter_tool(tag: str, den_path: Optional[str] = None) -> str:
    """
    View notes filtered by a specific tag.

    This shows only notes that have the specified tag. Notes are tagged using
    the format "note :: <TAG>" in the journal entry.

    Args:
        tag: The tag to filter notes by (e.g., "workout", "ideas", "meetings")
        den_path: Optional path to the den directory (defaults to /home/alex/personal/the-den)

    Returns:
        Notes matching the specified tag

    Examples:
        >>> notes_filter_tool("workout")
        "Notes with tag 'workout':\\n- Great session at gym\\n..."
        >>> notes_filter_tool("ideas")
        "Notes with tag 'ideas':\\n- New feature idea\\n..."
    """
    # Note: The 'daily' command uses its own default path
    if den_path and den_path != DEFAULT_DEN_PATH:
        command = ["daily", den_path, "--note", tag]
    else:
        command = ["daily", "--note", tag]

    return run_command(command, f"viewing notes with tag '{tag}'")

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


@tool
def habits_toggle_tool(
    habit_name: str, den_path: Optional[str] = None, offset: int = 0
) -> str:
    """
    Toggle the completion status of a habit checkbox.

    This toggles a habit between completed [x] and incomplete [ ] status.
    The habit name is matched case-insensitively.

    Args:
        habit_name: The name of the habit to toggle (e.g., "Gym", "Learn", "Track Macros")
        den_path: Optional path to the den directory (defaults to /home/alex/personal/the-den)
        offset: Number of days to offset from today (default: 0)

    Returns:
        Success message or error information

    Examples:
        >>> habits_toggle_tool("Gym")
        "✓ Toggled habit 'Gym'"
        >>> habits_toggle_tool("Learn", offset=-1)
        "✓ Toggled habit 'Learn'"
    """
    # Note: The 'daily' command uses its own default path
    if den_path and den_path != DEFAULT_DEN_PATH:
        command = ["daily", den_path, "--toggle-habit", habit_name]
    else:
        command = ["daily", "--toggle-habit", habit_name]

    if offset != 0:
        command.extend(["--offset", str(offset)])
    return run_command(command, f"toggling habit '{habit_name}'")


@tool
def tasks_entry_tool(
    task_text: str, den_path: Optional[str] = None, offset: int = 0
) -> str:
    """
    Add a task to the Today's Tasks section.

    This adds a new task with a checkbox to the Today's Tasks section.
    The task will have the format "- [ ] <task_text>".

    Args:
        task_text: The task description to add
        den_path: Optional path to the den directory (defaults to /home/alex/personal/the-den)
        offset: Number of days to offset from today (default: 0)

    Returns:
        Success message or error information

    Examples:
        >>> tasks_entry_tool("Review pull request")
        "✓ Added task to Today's Tasks"
        >>> tasks_entry_tool("Call dentist", offset=1)
        "✓ Added task to Today's Tasks"
    """
    # Note: The 'daily' command uses its own default path
    if den_path and den_path != DEFAULT_DEN_PATH:
        command = ["daily", den_path, "--task-entry", task_text]
    else:
        command = ["daily", "--task-entry", task_text]

    if offset != 0:
        command.extend(["--offset", str(offset)])
    return run_command(command, "adding task to Today's Tasks")


@tool
def tasks_tomorrow_entry_tool(
    task_text: str, den_path: Optional[str] = None, offset: int = 0
) -> str:
    """
    Add a task to the Tomorrow section.

    This adds a new task as a bullet point (without checkbox) to the Tomorrow section.
    The task will have the format "- <task_text>".

    Args:
        task_text: The task description to add
        den_path: Optional path to the den directory (defaults to /home/alex/personal/the-den)
        offset: Number of days to offset from today (default: 0)

    Returns:
        Success message or error information

    Examples:
        >>> tasks_tomorrow_entry_tool("Prepare presentation")
        "✓ Added task to Tomorrow"
        >>> tasks_tomorrow_entry_tool("Buy groceries", offset=1)
        "✓ Added task to Tomorrow"
    """
    # Note: The 'daily' command uses its own default path
    if den_path and den_path != DEFAULT_DEN_PATH:
        command = ["daily", den_path, "--task-tomorrow-entry", task_text]
    else:
        command = ["daily", "--task-tomorrow-entry", task_text]

    if offset != 0:
        command.extend(["--offset", str(offset)])
    return run_command(command, "adding task to Tomorrow")


@tool
def tasks_toggle_tool(
    task_index: int, den_path: Optional[str] = None, offset: int = 0
) -> str:
    """
    Toggle the completion status of a task by index.

    This toggles a task in Today's Tasks section between completed [x] and
    incomplete [ ] status. Tasks are indexed starting from 1.

    Args:
        task_index: The 1-based index of the task to toggle
        den_path: Optional path to the den directory (defaults to /home/alex/personal/the-den)
        offset: Number of days to offset from today (default: 0)

    Returns:
        Success message or error information

    Examples:
        >>> tasks_toggle_tool(1)
        "✓ Toggled task #1"
        >>> tasks_toggle_tool(2, offset=-1)
        "✓ Toggled task #2"
    """
    # Note: The 'daily' command uses its own default path
    if den_path and den_path != DEFAULT_DEN_PATH:
        command = ["daily", den_path, "--toggle-task", str(task_index)]
    else:
        command = ["daily", "--toggle-task", str(task_index)]

    if offset != 0:
        command.extend(["--offset", str(offset)])
    return run_command(command, f"toggling task #{task_index}")


@tool
def weight_entry_tool(
    weight_value: str, den_path: Optional[str] = None, offset: int = 0
) -> str:
    """
    Log weight for the day.

    This adds or updates the weight entry in the Weight section of the journal.
    If a weight entry already exists for the day, it will be updated with the new value.

    Args:
        weight_value: The weight value (e.g., "75", "75Kg", "75 Kg", "165lbs")
                     Note: Currently only Kg is supported in storage
        den_path: Optional path to the den directory (defaults to /home/alex/personal/the-den)
        offset: Number of days to offset from today (default: 0)

    Returns:
        Success message or error information

    Examples:
        >>> weight_entry_tool("75")
        "✓ Added/updated weight entry to 75 Kg"
        >>> weight_entry_tool("75Kg")
        "✓ Added/updated weight entry to 75 Kg"
        >>> weight_entry_tool("74.5", offset=-1)
        "✓ Added/updated weight entry to 74.5 Kg"
    """
    # Note: The 'daily' command uses its own default path
    if den_path and den_path != DEFAULT_DEN_PATH:
        command = ["daily", den_path, "--weight-entry", weight_value]
    else:
        command = ["daily", "--weight-entry", weight_value]

    if offset != 0:
        command.extend(["--offset", str(offset)])
    return run_command(command, f"adding/updating weight entry")


@tool
def tasks_remove_tool(
    task_index: int, den_path: Optional[str] = None, offset: int = 0
) -> str:
    """
    Remove a task from Today's Tasks section by index.

    This permanently removes a task from the Today's Tasks section.
    Tasks are indexed starting from 1.

    Args:
        task_index: The 1-based index of the task to remove
        den_path: Optional path to the den directory (defaults to /home/alex/personal/the-den)
        offset: Number of days to offset from today (default: 0)

    Returns:
        Success message or error information

    Examples:
        >>> tasks_remove_tool(1)
        "✓ Removed task #1 from Today's Tasks"
        >>> tasks_remove_tool(3, offset=-1)
        "✓ Removed task #3 from Today's Tasks"
    """
    # Note: The 'daily' command uses its own default path
    if den_path and den_path != DEFAULT_DEN_PATH:
        command = ["daily", den_path, "--task-remove", str(task_index)]
    else:
        command = ["daily", "--task-remove", str(task_index)]

    if offset != 0:
        command.extend(["--offset", str(offset)])
    return run_command(command, f"removing task #{task_index} from Today's Tasks")


@tool
def tasks_tomorrow_remove_tool(
    task_index: int, den_path: Optional[str] = None, offset: int = 0
) -> str:
    """
    Remove a task from Tomorrow section by index.

    This permanently removes a task from the Tomorrow section.
    Tasks are indexed starting from 1.

    Args:
        task_index: The 1-based index of the task to remove
        den_path: Optional path to the den directory (defaults to /home/alex/personal/the-den)
        offset: Number of days to offset from today (default: 0)

    Returns:
        Success message or error information

    Examples:
        >>> tasks_tomorrow_remove_tool(1)
        "✓ Removed task #1 from Tomorrow"
        >>> tasks_tomorrow_remove_tool(2, offset=-1)
        "✓ Removed task #2 from Tomorrow"
    """
    # Note: The 'daily' command uses its own default path
    if den_path and den_path != DEFAULT_DEN_PATH:
        command = ["daily", den_path, "--task-tomorrow-remove", str(task_index)]
    else:
        command = ["daily", "--task-tomorrow-remove", str(task_index)]

    if offset != 0:
        command.extend(["--offset", str(offset)])
    return run_command(command, f"removing task #{task_index} from Tomorrow")

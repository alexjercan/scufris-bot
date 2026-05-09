"""Local REPL-style CLI for the Scufris agent.

Lets you chat with the bot from a terminal without needing to deploy to
Telegram. Uses readline for line editing and rich for pretty output.

Run with:  uv run scufris-cli   (or: python cli.py)
"""

from __future__ import annotations

import asyncio
import atexit
import readline
import time
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from utils import (
    ThinkingEvent,
    ToolCallbackHandler,
    create_agent_manager,
    create_history_manager,
    load_config,
    setup_logging,
    setup_scufris,
    truncate_log,
)

# Pseudo user id used to scope history within the CLI session.
CLI_USER_ID = 0

HISTORY_FILE = Path.home() / ".scufris_cli_history"

HELP_TEXT = """\
Available commands:
  /help              Show this message
  /clear             Clear chat history for this session
  /history           Show chat history stats
  /multiline         Toggle multiline input (end with a single '.' line)
  /exit, /quit       Exit the REPL (Ctrl-D on empty line also works)

Anything else you type is sent to the agent.
"""


def _setup_readline() -> None:
    """Wire up persistent readline history."""
    try:
        readline.read_history_file(HISTORY_FILE)
    except FileNotFoundError:
        pass
    except OSError:
        # Corrupt history file or permission issue — ignore.
        pass

    readline.set_history_length(1000)
    atexit.register(_save_readline_history)


def _save_readline_history() -> None:
    try:
        readline.write_history_file(HISTORY_FILE)
    except OSError:
        pass


def _read_input(console: Console, multiline: bool) -> str | None:
    """Read a single user message from stdin.

    Returns None on EOF (Ctrl-D on empty input) so the caller can exit.
    """
    prompt = "[bold cyan]>[/bold cyan] "
    # rich's input() integrates nicely with readline.
    try:
        first = console.input(prompt)
    except EOFError:
        return None

    if not multiline:
        return first

    # Multiline mode: keep collecting until a line that is just "." (or EOF).
    lines = [first]
    while True:
        try:
            line = console.input("[dim]…[/dim] ")
        except EOFError:
            break
        if line.strip() == ".":
            break
        lines.append(line)
    return "\n".join(lines)


async def _handle_message(
    console: Console,
    agent_manager,
    history_manager,
    user_message: str,
    logger,
) -> None:
    """Send a single user message through the agent and render the reply."""
    request_start = time.time()
    logger.info(f"User CLI: {truncate_log(user_message, 100)}")

    try:
        messages = history_manager.get_history_with_new_message(
            CLI_USER_ID, user_message
        )

        process_start = time.time()
        response_text = await agent_manager.process_message(messages)
        process_duration = time.time() - process_start

        history_manager.add_user_message(CLI_USER_ID, user_message)
        history_manager.add_ai_message(CLI_USER_ID, response_text)

        total_duration = time.time() - request_start
        logger.info(
            f"CLI request done | total={total_duration:.2f}s "
            f"(process={process_duration:.2f}s) | response={len(response_text)} chars"
        )

        console.print(
            Panel(
                Markdown(response_text),
                title="[bold green]scufris[/bold green]",
                border_style="green",
            )
        )
    except Exception as e:  # noqa: BLE001 — surface anything to the user
        logger.error(f"Error processing CLI message: {e}", exc_info=True)
        console.print(f"[bold red]error:[/bold red] {e}")


def _handle_command(
    console: Console,
    history_manager,
    cmd: str,
    multiline: bool,
) -> tuple[bool, bool]:
    """Handle a /slash command.

    Returns (should_exit, new_multiline_state).
    """
    cmd = cmd.strip()
    if cmd in ("/exit", "/quit"):
        return True, multiline

    if cmd == "/help":
        console.print(HELP_TEXT)
        return False, multiline

    if cmd == "/clear":
        count = history_manager.get_message_count(CLI_USER_ID)
        history_manager.clear_history(CLI_USER_ID)
        console.print(f"[yellow]cleared {count} messages[/yellow]")
        return False, multiline

    if cmd == "/history":
        count = history_manager.get_message_count(CLI_USER_ID)
        stats = history_manager.get_stats()
        console.print(
            f"messages in this session: {count}\n"
            f"max per user: {stats['max_history_per_user']}\n"
            f"total users: {stats['total_users']}\n"
            f"total messages: {stats['total_messages']}"
        )
        return False, multiline

    if cmd == "/multiline":
        new_state = not multiline
        console.print(
            f"[yellow]multiline mode {'on' if new_state else 'off'}"
            f"[/yellow]"
            + (" — finish input with a single '.' line" if new_state else "")
        )
        return False, new_state

    console.print(f"[red]unknown command:[/red] {cmd}  (try /help)")
    return False, multiline


def main() -> None:
    import logging

    # CLI is a debugging tool — surface everything by default. Overridable
    # via the LOG_LEVEL env var.
    logger = setup_logging(default_level=logging.DEBUG)
    config = load_config(require_telegram=False)

    history_manager = create_history_manager(config.max_history_per_user)
    main_agent = setup_scufris(config=config)

    console = Console()
    _setup_readline()

    # Render "thinking" events as dim chat-style messages, indented to
    # mirror the agent → sub-agent → tool nesting.
    def render_thinking(ev: ThinkingEvent) -> None:
        indent = "  " * ev.depth
        if ev.kind == "tool_call":
            console.print(
                f"[dim]{indent}→ [cyan]{ev.source}[/cyan] calls "
                f"[bold]{ev.text}[/bold][/dim]"
            )
        elif ev.kind == "text":
            # Truncate very long reasoning so the chat stays readable;
            # the full text is still in the DEBUG log trace.
            snippet = truncate_log(ev.text.replace("\n", " "), 240)
            console.print(f"[dim italic]{indent}{ev.source}: {snippet}[/dim italic]")
        else:  # tool_result — currently unused, kept for completeness
            snippet = truncate_log(ev.text.replace("\n", " "), 240)
            console.print(f"[dim]{indent}↩ {snippet}[/dim]")

    # Register the callback handler so we get the same depth-aware
    # tool/sub-agent/LLM trace as the Telegram bot. No transport needed.
    callback_handler = ToolCallbackHandler(on_thinking=render_thinking)
    agent_manager = create_agent_manager(agent=main_agent, callbacks=[callback_handler])

    console.print(
        "[bold]Scufris CLI[/bold] — type [bold]/help[/bold] for commands, "
        "[bold]Ctrl-D[/bold] on empty line to exit."
    )

    multiline = False
    loop = asyncio.new_event_loop()
    try:
        while True:
            try:
                user_message = _read_input(console, multiline)
            except KeyboardInterrupt:
                console.print()  # break out of the current line cleanly
                continue

            if user_message is None:
                console.print("\n[dim]bye![/dim]")
                break

            stripped = user_message.strip()
            if not stripped:
                continue

            if stripped.startswith("/"):
                should_exit, multiline = _handle_command(
                    console, history_manager, stripped, multiline
                )
                if should_exit:
                    console.print("[dim]bye![/dim]")
                    break
                continue

            try:
                loop.run_until_complete(
                    _handle_message(
                        console, agent_manager, history_manager, user_message, logger
                    )
                )
            except KeyboardInterrupt:
                console.print("[yellow]interrupted[/yellow]")
    finally:
        loop.close()


if __name__ == "__main__":
    main()

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
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from utils import (
    ThinkingEvent,
    ToolCallbackHandler,
    create_agent_manager,
    create_history_manager,
    display_name,
    is_sub_agent,
    load_config,
    setup_logging,
    setup_scufris,
    truncate_log,
)
from utils.stats import format_stats_lines
from utils.telemetry import begin_turn

# Pseudo user id used to scope history within the CLI session.
CLI_USER_ID = 0

HISTORY_FILE = Path.home() / ".scufris_cli_history"

HELP_TEXT = """\
Available commands:
  /help              Show this message
  /clear             Clear chat history for this session
  /stats             Show per-agent memory breakdown
  /multiline         Toggle multiline input (end with a single '.' line)
  /thinking [full|short]
                     Show or toggle whether the agent's thinking text is
                     printed in full. Defaults to full; set
                     SCUFRIS_FULL_THINKING=0 to start in short mode
                     (240-char truncation).
  /exit, /quit       Exit the REPL (Ctrl-D on empty line also works)

Anything else you type is sent to the agent.
"""

# Length cap for "short" thinking text. Picked to fit a couple of wrapped
# lines on a typical terminal without dominating the chat scrollback.
THINKING_SHORT_LIMIT = 240


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
        with begin_turn(f"cli:{CLI_USER_ID}"):
            response_text = await agent_manager.process_message(messages, CLI_USER_ID)
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
    settings: dict,
) -> tuple[bool, bool]:
    """Handle a /slash command.

    `settings` is a mutable dict shared with the rest of the REPL —
    used here so `/thinking` can flip rendering state in place without
    threading a callback through every layer.

    Returns (should_exit, new_multiline_state).
    """
    cmd = cmd.strip()
    if cmd in ("/exit", "/quit"):
        return True, multiline

    if cmd == "/help":
        console.print(HELP_TEXT)
        return False, multiline

    if cmd == "/clear":
        breakdown = history_manager.get_user_breakdown(CLI_USER_ID)
        total = history_manager.clear_history(CLI_USER_ID)
        if total == 0:
            console.print("[yellow]no messages to clear[/yellow]")
        elif breakdown:
            breakdown_str = ", ".join(f"{a}: {n}" for a, n in sorted(breakdown.items()))
            console.print(
                f"[yellow]cleared {total} messages ({breakdown_str})[/yellow]"
            )
        else:
            console.print(f"[yellow]cleared {total} messages[/yellow]")
        return False, multiline

    if cmd == "/stats":
        lines = format_stats_lines(
            history_manager,
            CLI_USER_ID,
            started_at=settings["started_at"],
            model=settings["model"],
            base_url=settings["base_url"],
        )
        console.print("\n".join(lines))
        return False, multiline

    if cmd == "/multiline":
        new_state = not multiline
        console.print(
            f"[yellow]multiline mode {'on' if new_state else 'off'}"
            f"[/yellow]"
            + (" — finish input with a single '.' line" if new_state else "")
        )
        return False, new_state

    if cmd.startswith("/thinking"):
        parts = cmd.split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        if arg == "full":
            settings["full_thinking"] = True
        elif arg in ("short", "truncate", "off"):
            settings["full_thinking"] = False
        elif arg == "":
            # No arg = toggle.
            settings["full_thinking"] = not settings.get("full_thinking", False)
        else:
            console.print(
                f"[red]unknown /thinking arg:[/red] {arg}  (use 'full' or 'short')"
            )
            return False, multiline
        mode = "full" if settings["full_thinking"] else "short"
        console.print(f"[yellow]thinking mode: {mode}[/yellow]")
        return False, multiline

    console.print(f"[red]unknown command:[/red] {cmd}  (try /help)")
    return False, multiline


def main() -> None:
    import argparse
    import logging
    import os

    parser = argparse.ArgumentParser(
        prog="scufris-cli",
        description="Interactive CLI for the Scufris agent.",
    )
    parser.add_argument(
        "--short-thinking",
        action="store_true",
        help=(
            "Start with truncated (short) thinking output. Overrides "
            "SCUFRIS_FULL_THINKING. Toggle live with /thinking."
        ),
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help=(
            "Silence logging — only ERROR-and-above are shown. Useful "
            "when you just want the chat output. Overrides LOG_LEVEL."
        ),
    )
    args = parser.parse_args()

    # CLI is a debugging tool — surface everything by default. `--quiet`
    # bumps the floor to ERROR; LOG_LEVEL env var still wins if neither
    # is set. `level=` overrides both env and default in setup_logging.
    if args.quiet:
        logger = setup_logging(level=logging.ERROR)
    else:
        logger = setup_logging(default_level=logging.DEBUG)
    config = load_config(require_telegram=False)

    history_manager = create_history_manager(config.max_history_per_user)
    main_agent = setup_scufris(config=config, history_manager=history_manager)

    console = Console()
    _setup_readline()

    # Mutable settings shared with the slash-command handler and the
    # thinking renderer. Default is full thinking — the CLI is a debug
    # tool, so showing everything is the most useful baseline. CLI flag
    # `--short-thinking` wins; otherwise SCUFRIS_FULL_THINKING=0/false/no
    # /off starts in short mode.
    if args.short_thinking:
        full_thinking = False
    else:
        full_env = os.environ.get("SCUFRIS_FULL_THINKING", "").lower()
        full_thinking = full_env not in ("0", "false", "no", "off")
    settings: dict = {
        "full_thinking": full_thinking,
        "started_at": datetime.now(timezone.utc),
        "model": config.ollama_model,
        "base_url": config.ollama_base_url,
    }

    # Render "thinking" events as dim chat-style messages, indented to
    # mirror the agent → sub-agent → tool nesting.
    def render_thinking(ev: ThinkingEvent) -> None:
        indent = "  " * ev.depth
        src = display_name(ev.source)
        if ev.kind == "tool_call":
            target = display_name(ev.text)
            verb = "asks" if is_sub_agent(ev.text) else "uses"
            line = f"{indent}→ [cyan]{src}[/cyan] {verb} [bold]{target}[/bold]"
            if ev.arg:
                line += f": [grey50]{ev.arg}[/grey50]"
            console.print(line)
            # Phase-2: surface the `context` briefing on its own line so
            # bad/good delegations are easy to eyeball. Indented one level
            # past the tool-call line so the visual nesting is obvious.
            # In short-thinking mode, truncate to the same limit used for
            # text events — keeps the trace scannable without losing the
            # signal that *some* context was passed.
            if ev.context:
                ctx = ev.context.replace("\n", " ")
                if not settings.get("full_thinking"):
                    ctx = truncate_log(ctx, THINKING_SHORT_LIMIT)
                console.print(f"{indent}  [grey50]↳ context: {ctx}[/grey50]")
        elif ev.kind == "tool_meta":
            # Phase 3.5 — `↳ +N prior turns` for sub-agents that loaded
            # history for this call. Emitted from on_tool_end, so it
            # lands after the nested trace; that's intentional (option B
            # in the task design — readability beats strict ordering).
            if ev.prior_turns and ev.prior_turns > 0:
                console.print(
                    f"{indent}  [grey50]↳ +{ev.prior_turns} prior turns[/grey50]"
                )
        elif ev.kind == "text":
            # In `short` mode, keep the chat scannable; full text is also
            # in the DEBUG log. In `full` mode, print everything verbatim.
            # Avoid `dim italic` — kitty+tmux renders it with a grey bg.
            text = ev.text.replace("\n", " ")
            if not settings.get("full_thinking"):
                text = truncate_log(text, THINKING_SHORT_LIMIT)
            console.print(f"{indent}[cyan]{src}[/cyan] [grey50]{text}[/grey50]")
        else:  # tool_result — currently unused, kept for completeness
            text = ev.text.replace("\n", " ")
            if not settings.get("full_thinking"):
                text = truncate_log(text, THINKING_SHORT_LIMIT)
            console.print(f"{indent}[grey50]↩ {text}[/grey50]")

    # Register the callback handler so we get the same depth-aware
    # tool/sub-agent/LLM trace as the Telegram bot. No transport needed.
    callback_handler = ToolCallbackHandler(on_thinking=render_thinking)
    agent_manager = create_agent_manager(
        agent=main_agent,
        callbacks=[callback_handler],
        history_manager=history_manager,
    )

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
                    console, history_manager, stripped, multiline, settings
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

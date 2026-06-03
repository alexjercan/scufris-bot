"""Local REPL CLI for the Scufris agent — HTTP client of ``scufris-server``.

The bot's brain (agent runtime, history, tools) lives in the daemon; this
module is just the terminal UX. Killing and restarting the CLI doesn't
evict your conversation — that lives in the daemon.

Connection settings:
  * ``SCUFRIS_SERVER_URL`` — base URL of the daemon
    (default ``http://127.0.0.1:8765``).
  * ``SCUFRIS_TOKEN`` — bearer token, only required when the server is
    configured with one.
  * ``SCUFRIS_USER`` — string identity used to derive a stable user id;
    falls back to ``getpass.getuser()``.
  * ``SCUFRIS_USER_ID`` — explicit integer override (wins over
    ``SCUFRIS_USER``).
  * ``SCUFRIS_FULL_THINKING`` — start in full-thinking render mode (default).

Run with:  uv run scufris-cli
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import getpass
import logging
import os
import readline
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from scufris_client import (
    ScufrisAuthError,
    ScufrisClient,
    ScufrisConnectionError,
    ScufrisError,
    ScufrisServerError,
    user_id_for,
)
from utils import (
    ThinkingEvent,
    display_name,
    is_sub_agent,
    load_config,
    setup_logging,
    truncate_log,
)

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


# ----------------------------------------------------------------------
# Readline plumbing (unchanged from the legacy CLI)
# ----------------------------------------------------------------------


def _setup_readline() -> None:
    try:
        readline.read_history_file(HISTORY_FILE)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    readline.set_history_length(1000)
    atexit.register(_save_readline_history)


def _save_readline_history() -> None:
    try:
        readline.write_history_file(HISTORY_FILE)
    except OSError:
        pass


def _read_input(console: Console, multiline: bool) -> str | None:
    prompt = "[bold cyan]>[/bold cyan] "
    try:
        first = console.input(prompt)
    except EOFError:
        return None
    if not multiline:
        return first
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


# ----------------------------------------------------------------------
# Thinking renderer — same shape as the legacy CLI so the visual output
# is identical. We just feed it ThinkingEvents from the SSE stream.
# ----------------------------------------------------------------------


def make_render_thinking(console: Console, settings: dict):
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
            if ev.context:
                ctx = ev.context.replace("\n", " ")
                if not settings.get("full_thinking"):
                    ctx = truncate_log(ctx, THINKING_SHORT_LIMIT)
                console.print(f"{indent}  [grey50]↳ context: {ctx}[/grey50]")
        elif ev.kind == "tool_meta":
            if ev.prior_turns and ev.prior_turns > 0:
                console.print(
                    f"{indent}  [grey50]↳ +{ev.prior_turns} prior turns[/grey50]"
                )
        elif ev.kind == "compaction":
            n_msg = ev.evicted or 0
            n_facts = ev.new_facts or 0
            console.print(
                f"[grey50][memory] {ev.source}: compacted {n_msg} msg(s), "
                f"+{n_facts} fact(s)[/grey50]"
            )
        elif ev.kind == "text":
            text = ev.text.replace("\n", " ")
            if not settings.get("full_thinking"):
                text = truncate_log(text, THINKING_SHORT_LIMIT)
            console.print(f"{indent}[cyan]{src}[/cyan] [grey50]{text}[/grey50]")
        else:  # tool_result — currently unused, kept for completeness
            text = ev.text.replace("\n", " ")
            if not settings.get("full_thinking"):
                text = truncate_log(text, THINKING_SHORT_LIMIT)
            console.print(f"{indent}[grey50]↩ {text}[/grey50]")

    return render_thinking


# ----------------------------------------------------------------------
# Per-turn handler — drives the SSE stream into the renderer.
# ----------------------------------------------------------------------


async def _handle_message(
    console: Console,
    client: ScufrisClient,
    user_id: int,
    user_message: str,
    render_thinking,
    logger: logging.Logger,
) -> None:
    request_start = time.time()
    logger.debug(f"User CLI: {truncate_log(user_message, 100)}")

    final_text: str | None = None
    error_text: str | None = None
    try:
        async for ev in client.chat_stream(user_id, user_message):
            if ev.kind == "thinking" and ev.thinking is not None:
                render_thinking(ev.thinking)
            elif ev.kind == "done":
                final_text = ev.text or ""
            elif ev.kind == "error":
                error_text = ev.error or "unknown error"
                break
    except ScufrisConnectionError as exc:
        console.print(
            f"[bold red]server unreachable:[/bold red] {exc}\n"
            "[dim]hint: is `scufris-server` running and is "
            "$SCUFRIS_SERVER_URL set correctly?[/dim]"
        )
        return
    except ScufrisAuthError as exc:
        console.print(
            f"[bold red]auth failed:[/bold red] {exc}\n"
            "[dim]hint: check $SCUFRIS_TOKEN[/dim]"
        )
        return
    except ScufrisServerError as exc:
        console.print(f"[bold red]server error:[/bold red] {exc}")
        return
    except asyncio.CancelledError:
        # Ctrl-C during the stream. Closing the generator above tears
        # down the SSE connection, which the server treats as a cancel.
        console.print("[yellow]interrupted — server canceled[/yellow]")
        return

    total_duration = time.time() - request_start
    if error_text is not None:
        console.print(f"[bold red]error:[/bold red] {error_text}")
        return
    if final_text is None:
        console.print("[bold red]stream ended without a `done` event[/bold red]")
        return

    logger.debug(
        f"CLI request done | total={total_duration:.2f}s | "
        f"response={len(final_text)} chars"
    )
    console.print(
        Panel(
            Markdown(final_text),
            title="[bold green]scufris[/bold green]",
            border_style="green",
        )
    )


# ----------------------------------------------------------------------
# Slash commands.
# ----------------------------------------------------------------------


async def _handle_command(
    console: Console,
    client: ScufrisClient,
    user_id: int,
    cmd: str,
    multiline: bool,
    settings: dict,
) -> tuple[bool, bool]:
    """Handle a /slash command. Returns (should_exit, new_multiline)."""
    cmd = cmd.strip()
    if cmd in ("/exit", "/quit"):
        return True, multiline

    if cmd == "/help":
        console.print(HELP_TEXT)
        return False, multiline

    if cmd == "/clear":
        try:
            result = await client.clear(user_id)
        except ScufrisError as exc:
            console.print(f"[bold red]clear failed:[/bold red] {exc}")
            return False, multiline
        cleared = result.get("cleared", 0)
        breakdown = result.get("breakdown") or {}
        if cleared == 0:
            console.print("[yellow]no messages to clear[/yellow]")
        elif breakdown:
            breakdown_str = ", ".join(f"{a}: {n}" for a, n in sorted(breakdown.items()))
            console.print(
                f"[yellow]cleared {cleared} messages ({breakdown_str})[/yellow]"
            )
        else:
            console.print(f"[yellow]cleared {cleared} messages[/yellow]")
        return False, multiline

    if cmd == "/stats":
        try:
            result = await client.stats(user_id)
        except ScufrisError as exc:
            console.print(f"[bold red]stats failed:[/bold red] {exc}")
            return False, multiline
        for line in result.get("lines", []):
            console.print(line)
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
        parts = cmd.split()
        if len(parts) == 1:
            mode = "full" if settings.get("full_thinking") else "short"
            console.print(f"[yellow]thinking mode: {mode}[/yellow]")
            return False, multiline
        choice = parts[1].lower()
        if choice == "full":
            settings["full_thinking"] = True
        elif choice == "short":
            settings["full_thinking"] = False
        else:
            console.print(f"[red]unknown thinking mode: {choice}[/red]")
            return False, multiline
        console.print(f"[yellow]thinking mode set to {choice}[/yellow]")
        return False, multiline

    console.print(f"[red]unknown command: {cmd}[/red] — try /help")
    return False, multiline


# ----------------------------------------------------------------------
# Entrypoint.
# ----------------------------------------------------------------------


async def _amain(args: argparse.Namespace) -> None:
    if args.quiet:
        logger = setup_logging(level=logging.ERROR)
    else:
        logger = setup_logging(default_level=logging.INFO)

    config = load_config(require_telegram=False)
    base_url = config.client.server_url
    token = config.server.token

    # Identity resolution order:
    #   1. SCUFRIS_USER_ID  — explicit numeric override (skip server call).
    #   2. SCUFRIS_USER     — surface_id sent to /v1/identity/resolve.
    #   3. getpass.getuser() — same fallback the legacy CLI used.
    # The local user_id_for() hash is still our offline fallback if the
    # resolve call fails (network / older server) so the REPL stays
    # usable even when identity isn't reachable.
    explicit_user_id = os.environ.get("SCUFRIS_USER_ID")
    cli_surface_id = os.environ.get("SCUFRIS_USER") or getpass.getuser()

    console = Console()
    _setup_readline()

    # `--short-thinking` is a per-invocation argparse flag and always
    # wins over the persistent default in [client].full_thinking.
    full_thinking = config.client.full_thinking and not args.short_thinking
    settings: dict = {
        "full_thinking": full_thinking,
        "started_at": datetime.now(timezone.utc),
    }
    render_thinking = make_render_thinking(console, settings)

    multiline = False
    async with ScufrisClient(base_url=base_url, token=token) as client:
        # Probe the server up front so connection problems are surfaced
        # before the user types anything.
        try:
            await client.healthz()
        except ScufrisConnectionError as exc:
            console.print(
                f"[bold red]server unreachable:[/bold red] {exc}\n"
                "[dim]start it with `scufris-server` then retry.[/dim]"
            )
            return
        except ScufrisAuthError as exc:
            # /healthz is unauth on the server, but be defensive anyway.
            console.print(f"[bold red]auth failed:[/bold red] {exc}")
            return
        except ScufrisError as exc:
            console.print(f"[bold red]error:[/bold red] {exc}")
            return

        # Resolve our identity now that the server is up. The result
        # may include a friendly username and the list of bound
        # surfaces (so we can show "Telegram is linked" in the banner).
        username: Optional[str] = None
        bound_surfaces: list[str] = []
        if explicit_user_id:
            try:
                user_id = int(explicit_user_id)
            except ValueError:
                console.print(
                    f"[bold red]SCUFRIS_USER_ID must be an integer, got "
                    f"{explicit_user_id!r}[/bold red]"
                )
                return
        else:
            try:
                resolved = await client.resolve_identity("cli", cli_surface_id)
                user_id = int(resolved["user_id"])
                username = resolved.get("username")
                bound_surfaces = list(resolved.get("bound_surfaces") or [])
            except ScufrisError as exc:
                # Stay usable offline-ish: fall back to the local hash so
                # we can still chat if the resolve endpoint is missing
                # (older server) or has a transient hiccup.
                logger.warning("identity resolve failed (%s); falling back", exc)
                user_id = user_id_for(cli_surface_id)

        identity_blurb = f"as [dim]user {user_id}[/dim]"
        if username:
            identity_blurb = f"as [bold]{username}[/bold] [dim](user {user_id})[/dim]"
        if bound_surfaces and bound_surfaces != ["cli"]:
            other = [s for s in bound_surfaces if s != "cli"]
            if other:
                identity_blurb += f" — linked surfaces: {', '.join(other)}"

        console.print(
            f"[bold]Scufris CLI[/bold] → [dim]{base_url}[/dim] {identity_blurb} — "
            "type [bold]/help[/bold] for commands, "
            "[bold]Ctrl-D[/bold] on empty line to exit."
        )

        while True:
            try:
                user_message = await asyncio.to_thread(_read_input, console, multiline)
            except KeyboardInterrupt:
                console.print()
                continue

            if user_message is None:
                console.print("\n[dim]bye![/dim]")
                break

            stripped = user_message.strip()
            if not stripped:
                continue

            if stripped.startswith("/"):
                should_exit, multiline = await _handle_command(
                    console, client, user_id, stripped, multiline, settings
                )
                if should_exit:
                    console.print("[dim]bye![/dim]")
                    break
                continue

            try:
                await _handle_message(
                    console,
                    client,
                    user_id,
                    user_message,
                    render_thinking,
                    logger,
                )
            except KeyboardInterrupt:
                console.print("[yellow]interrupted[/yellow]")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scufris-cli",
        description="Interactive CLI for the Scufris agent (HTTP client).",
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
        help="Silence logging — only ERROR-and-above are shown.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

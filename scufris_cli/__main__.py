"""scufris-cli v2 — terminal REPL over the scufris-server v2 HTTP surface.

The agent runtime, session history, and tools all live inside
``scufris-server``; this module is purely the terminal UX. Restarting
the CLI never evicts your conversation — state lives in the daemon.

Connection settings (env vars):
  SCUFRIS_SERVER_URL   Base URL of the daemon (default http://127.0.0.1:7080).
  SCUFRIS_USER         surface_id sent to /v1/identity/resolve.
                       Falls back to getpass.getuser().
  SCUFRIS_FULL_THINKING  "1" → start in full mode (default), "0" → short.

Dropped from v1: SCUFRIS_TOKEN (no auth in v2 per ADR-8),
SCUFRIS_USER_ID (server resolves user_id from surface+surface_id now).

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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from scufris_client import (
    ScufrisAuthError,
    ScufrisClient,
    ScufrisConnectionError,
    ScufrisError,
    ScufrisServerError,
    ThinkingEvent,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HISTORY_FILE = Path.home() / ".scufris_cli_history"

#: Length cap for "short" thinking text — fits ~2 wrapped lines.
THINKING_SHORT_LIMIT = 240

#: The agent name sent on every chat turn. Hardcoded per #14 D3;
#: a /agent command + --agent flag is a separate follow-up task.
_AGENT = "build"

HELP_TEXT = """\
[bold]Available commands:[/bold]

  [cyan]/help[/cyan]                   Show this message
  [cyan]/sessions[/cyan]               List all your channels with last-used time
  [cyan]/clear[/cyan]                  Clear every channel for your user (all history)
  [cyan]/thinking[/cyan] [dim][full|short][/dim]    Show or toggle thinking render mode
  [cyan]/multiline[/cyan]              Toggle multiline input (submit with a lone [bold].[/bold])
  [cyan]/exit[/cyan], [cyan]/quit[/cyan]            Exit the REPL (Ctrl-D on empty line also works)

Anything else is sent to the agent.
"""

# ---------------------------------------------------------------------------
# Tiny helpers (inlined from v1's utils — avoids the utils dep in the client)
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    """Truncate *text* to *limit* chars, appending '…' if clipped."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _display_name(raw: str) -> str:
    """Convert snake_case / dot.separated tool names to Title Case words."""
    # e.g. "bash_tool" → "Bash Tool", "fs.read" → "Fs Read"
    return " ".join(w.capitalize() for w in raw.replace(".", "_").split("_"))


# ---------------------------------------------------------------------------
# Settings bag (mutable, shared across the session)
# ---------------------------------------------------------------------------


@dataclass
class _Settings:
    full_thinking: bool = True
    started_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.started_at is None:
            self.started_at = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Readline plumbing
# ---------------------------------------------------------------------------


def _setup_readline() -> None:
    try:
        readline.read_history_file(HISTORY_FILE)
    except (FileNotFoundError, OSError):
        pass
    readline.set_history_length(1000)
    atexit.register(_save_readline_history)


def _save_readline_history() -> None:
    try:
        readline.write_history_file(HISTORY_FILE)
    except OSError:
        pass


def _read_input(console: Console, multiline: bool) -> str | None:
    """Read one user turn from the terminal. Returns None on EOF (Ctrl-D)."""
    prompt = "> "
    try:
        first = input(prompt)
    except EOFError:
        return None
    except KeyboardInterrupt:
        # Surface Ctrl-C so the caller can blank the line and continue.
        raise

    if not multiline:
        return first

    lines = [first]
    while True:
        try:
            line = input("… ")
        except EOFError:
            break
        if line.strip() == ".":
            break
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Thinking renderer (D5)
# ---------------------------------------------------------------------------


def make_render_thinking(
    console: Console, settings: _Settings
) -> "Callable[[ThinkingEvent], None]":  # noqa: F821 — forward ref for docstring only
    """Return a render function that pretty-prints one ThinkingEvent.

    Branches match exactly what v2's mapper emits (D5):
      tool_call   — cyan arrow + tool name + optional arg
      tool_result — green tick (success) or red prefix (failure)
      tool_meta   — dim grey annotation (permissions, notes)
      text        — dim grey streaming token chunk

    Dropped vs v1: ``compaction`` branch, ``prior_turns``/``context``
    conditionals, ``is_sub_agent`` verb split (depth always 0 in v2).
    """

    def render(ev: ThinkingEvent) -> None:
        indent = "  " * ev.depth  # always "" in v2 (depth==0), but defensive
        src = _display_name(ev.source)

        if ev.kind == "tool_call":
            target = _display_name(ev.text)
            # v2 has no sub-agents; hardcode "uses". Kept as a helper call
            # so re-enabling the verb split later is a one-liner.
            verb = "uses"
            line = f"{indent}→ [cyan]{src}[/cyan] {verb} [bold]{target}[/bold]"
            if ev.arg:
                arg_text = _truncate(ev.arg, THINKING_SHORT_LIMIT)
                line += f": [grey50]{arg_text}[/grey50]"
            console.print(line)

        elif ev.kind == "tool_result":
            # First-class in v2 (D5). Server emits one per completed/error tool.
            text = ev.text.replace("\n", " ")
            if not settings.full_thinking:
                text = _truncate(text, THINKING_SHORT_LIMIT)
            # Failure heuristic: server formats error results as "<tool> failed: …"
            if "failed:" in text:
                console.print(f"{indent}[red]✗ {text}[/red]")
            else:
                tool_name = _display_name(ev.text.split("\n")[0])
                console.print(f"{indent}[green]↩ {tool_name}[/green]")

        elif ev.kind == "tool_meta":
            # Permission events, annotations, notes. Always dim.
            text = ev.text.replace("\n", " ")
            if not settings.full_thinking:
                text = _truncate(text, THINKING_SHORT_LIMIT)
            console.print(f"{indent}  [grey50]ℹ {text}[/grey50]")

        elif ev.kind == "text":
            # Streaming token chunk — show as a grey delta.
            text = ev.text.replace("\n", " ")
            if not settings.full_thinking:
                text = _truncate(text, THINKING_SHORT_LIMIT)
            console.print(f"{indent}[grey50]{text}[/grey50]", end="")

        # Defensive: unknown kinds (e.g. future server additions) are
        # silently dropped — the renderer ignores what it can't handle
        # rather than crashing mid-stream.

    return render


# ---------------------------------------------------------------------------
# Per-turn handler
# ---------------------------------------------------------------------------


async def _handle_message(
    console: Console,
    client: ScufrisClient,
    surface_id: str,
    message: str,
    render_thinking: "Callable[[ThinkingEvent], None]",  # noqa: F821
    logger: logging.Logger,
) -> None:
    """Drive one chat turn: open the SSE stream, render events, print reply."""
    logger.debug("user turn: %s", _truncate(message, 120))

    final_text: str | None = None
    error_text: str | None = None
    error_type: str | None = None

    try:
        async for ev in client.chat_stream(
            surface="cli",
            surface_id=surface_id,
            agent=_AGENT,
            message=message,
        ):
            if ev.kind == "thinking" and ev.thinking is not None:
                render_thinking(ev.thinking)
            elif ev.kind == "done":
                final_text = ev.text or ""
                logger.debug(
                    "done — session=%s tokens=%s cost=%s",
                    ev.oc_session_id,
                    ev.tokens,
                    ev.cost,
                )
            elif ev.kind == "error":
                error_text = ev.error or "unknown error"
                error_type = ev.error_type
                break

        console.print()  # newline after the last thinking chunk

    except ScufrisConnectionError as exc:
        console.print(
            f"\n[bold red]✗ server unreachable:[/bold red] {exc}\n"
            "[dim]Hint: is `scufris-server` running? "
            "Check $SCUFRIS_SERVER_URL.[/dim]"
        )
        return
    except ScufrisAuthError as exc:
        console.print(f"\n[bold red]✗ auth failed:[/bold red] {exc}")
        return
    except ScufrisServerError as exc:
        console.print(f"\n[bold red]✗ server error:[/bold red] {exc}")
        return
    except asyncio.CancelledError:
        console.print("\n[yellow]interrupted — server canceled[/yellow]")
        return

    if error_text is not None:
        console.print(f"\n[bold red]✗ {error_text}[/bold red]", highlight=False)
        if error_type:
            console.print(f"[dim]  type: {error_type}[/dim]")
        return

    if final_text is None:
        console.print("[bold red]stream ended without a `done` event[/bold red]")
        return

    console.print(
        Panel(
            Markdown(final_text),
            title="[bold green]scufris[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Slash commands (D4)
# ---------------------------------------------------------------------------


async def _handle_command(
    console: Console,
    client: ScufrisClient,
    user_id: int | None,
    surface_id: str,
    cmd: str,
    multiline: bool,
    settings: _Settings,
) -> tuple[bool, bool]:
    """Dispatch a /slash command. Returns ``(should_exit, new_multiline)``."""
    parts = cmd.split()
    name = parts[0].lower()

    # ── /exit /quit ──────────────────────────────────────────────────────────
    if name in ("/exit", "/quit"):
        return True, multiline

    # ── /help ────────────────────────────────────────────────────────────────
    if name == "/help":
        console.print(HELP_TEXT)
        return False, multiline

    # ── /sessions ────────────────────────────────────────────────────────────
    if name == "/sessions":
        if user_id is None:
            console.print(
                "[yellow]identity unavailable — /sessions requires a resolved "
                "user_id. Restart the CLI when the server is reachable.[/yellow]"
            )
            return False, multiline
        try:
            channels: list[dict[str, Any]] = await client.sessions(user_id)
        except ScufrisError as exc:
            console.print(f"[bold red]sessions failed:[/bold red] {exc}")
            return False, multiline

        channels.sort(key=lambda x: x.get("last_used_at") or 0, reverse=True)

        if not channels:
            console.print("[dim]no sessions yet[/dim]")
            return False, multiline

        table = Table(
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold cyan",
            padding=(0, 1),
        )
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Surface / user / agent")
        table.add_column("Last used", no_wrap=True)

        for ch in channels:
            ch_id = str(ch.get("channel_id", "?"))
            ch_info = ch.get("channel") or {}
            surface = ch_info.get("surface", "?")
            sid = ch_info.get("surface_id", "?")
            agent = ch_info.get("agent", "?")
            last_used_ts = ch.get("last_used_at")
            if last_used_ts:
                try:
                    last_used = datetime.fromtimestamp(
                        int(last_used_ts), tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M UTC")
                except (ValueError, OSError):
                    last_used = str(last_used_ts)
            else:
                last_used = "—"
            table.add_row(ch_id, f"{surface}/{sid}/{agent}", last_used)

        console.print(table)
        return False, multiline

    # ── /clear ───────────────────────────────────────────────────────────────
    if name == "/clear":
        if user_id is None:
            console.print(
                "[yellow]identity unavailable — /clear requires a resolved "
                "user_id. Restart the CLI when the server is reachable.[/yellow]"
            )
            return False, multiline
        try:
            result = await client.clear(user_id)
        except ScufrisError as exc:
            console.print(f"[bold red]clear failed:[/bold red] {exc}")
            return False, multiline
        count = result.get("count", 0)
        if count == 0:
            console.print("[yellow]no sessions to clear[/yellow]")
        else:
            console.print(f"[yellow]cleared {count} session(s)[/yellow]")
        return False, multiline

    # ── /thinking [full|short] ───────────────────────────────────────────────
    if name == "/thinking":
        if len(parts) == 1:
            mode = "full" if settings.full_thinking else "short"
            console.print(f"[yellow]thinking mode: {mode}[/yellow]")
            return False, multiline
        choice = parts[1].lower()
        if choice == "full":
            settings.full_thinking = True
            console.print("[yellow]thinking mode: full[/yellow]")
        elif choice == "short":
            settings.full_thinking = False
            console.print(
                f"[yellow]thinking mode: short "
                f"[dim]({THINKING_SHORT_LIMIT} chars)[/dim][/yellow]"
            )
        else:
            console.print(
                f"[red]unknown thinking mode:[/red] {choice!r} "
                "(use [bold]full[/bold] or [bold]short[/bold])"
            )
        return False, multiline

    # ── /multiline ───────────────────────────────────────────────────────────
    if name == "/multiline":
        new_state = not multiline
        if new_state:
            console.print(
                "[yellow]multiline mode: on[/yellow] "
                "[dim]— finish input with a lone . on its own line[/dim]"
            )
        else:
            console.print("[yellow]multiline mode: off[/yellow]")
        return False, new_state

    # ── unknown ──────────────────────────────────────────────────────────────
    console.print(f"[red]unknown command:[/red] {cmd!r} — try [bold]/help[/bold]")
    return False, multiline



# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------


async def _amain(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.ERROR if args.quiet else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("scufris_cli")

    base_url = os.environ.get("SCUFRIS_SERVER_URL", "http://127.0.0.1:7080")
    cli_surface_id = os.environ.get("SCUFRIS_USER") or getpass.getuser()
    full_thinking_env = os.environ.get("SCUFRIS_FULL_THINKING", "1") != "0"
    settings = _Settings(
        full_thinking=full_thinking_env and not args.short_thinking,
    )

    console = Console()
    _setup_readline()

    async with ScufrisClient(base_url=base_url) as client:
        # ── Liveness probe ───────────────────────────────────────────────────
        try:
            await client.healthz()
        except ScufrisConnectionError as exc:
            console.print(
                f"[bold red]✗ server unreachable:[/bold red] {exc}\n"
                "[dim]Start the daemon with `scufris-server`, or set "
                "$SCUFRIS_SERVER_URL.[/dim]"
            )
            raise SystemExit(1) from exc
        except ScufrisError as exc:
            console.print(f"[bold red]✗ error:[/bold red] {exc}")
            raise SystemExit(1) from exc

        # ── Identity resolve (D9: degrade, don't bail) ───────────────────────
        user_id: int | None = None
        username: str | None = None
        bound_surfaces: list[str] = []

        try:
            resolved = await client.resolve_identity("cli", cli_surface_id)
            user_id = int(resolved["user_id"])
            username = resolved.get("username")
            bound_surfaces = [
                b.get("surface", "?")
                for b in (resolved.get("bound_surfaces") or [])
                if b.get("surface") != "cli"
            ]
        except ScufrisError as exc:
            logger.warning("identity resolve failed (%s); degraded mode", exc)

        # ── Banner ───────────────────────────────────────────────────────────
        if user_id is not None:
            if username:
                who = f"[bold]{username}[/bold] [dim](user {user_id})[/dim]"
            else:
                who = f"[dim]user {user_id}[/dim]"
        else:
            who = "[dim]user unknown[/dim]"

        banner = f"[bold]Scufris CLI[/bold] → [dim]{base_url}[/dim] as {who}"
        if bound_surfaces:
            banner += f" — linked surfaces: [dim]{', '.join(bound_surfaces)}[/dim]"
        banner += (
            " — type [bold]/help[/bold] for commands, "
            "[bold]Ctrl-D[/bold] on empty line to exit."
        )
        console.rule(style="green")
        console.print(banner)
        console.rule(style="green")

        render_thinking = make_render_thinking(console, settings)
        multiline = False

        # ── REPL loop ────────────────────────────────────────────────────────
        while True:
            try:
                user_message = await asyncio.to_thread(_read_input, console, multiline)
            except KeyboardInterrupt:
                # Ctrl-C at the prompt: blank line, continue.
                console.print()
                continue

            if user_message is None:
                # Ctrl-D on empty input: clean exit.
                console.print("\n[dim]bye![/dim]")
                break

            stripped = user_message.strip()
            if not stripped:
                continue

            if stripped.startswith("/"):
                should_exit, multiline = await _handle_command(
                    console,
                    client,
                    user_id,
                    cli_surface_id,
                    stripped,
                    multiline,
                    settings,
                )
                if should_exit:
                    console.print("[dim]bye![/dim]")
                    break
                continue

            try:
                await _handle_message(
                    console,
                    client,
                    cli_surface_id,
                    stripped,
                    render_thinking,
                    logger,
                )
            except KeyboardInterrupt:
                console.print("\n[yellow]interrupted[/yellow]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scufris-cli",
        description="Interactive REPL for the Scufris agent (HTTP v2 client).",
    )
    parser.add_argument(
        "--short-thinking",
        action="store_true",
        help=(
            f"Start with truncated thinking output ({THINKING_SHORT_LIMIT} chars). "
            "Overrides SCUFRIS_FULL_THINKING=1. Toggle live with /thinking."
        ),
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress all log output below ERROR level.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

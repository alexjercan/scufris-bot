"""scufris-bot v2 — Telegram front-end over the scufris-server v2 HTTP surface.

The agent runtime, session history, and tools all live inside
``scufris-server``; this module is purely the Telegram UX, mirroring
``cli.py``'s relationship to the server. Restarting the bot never
evicts anyone's conversation — state lives in the daemon.

v2 NOTE: this is a from-scratch self-contained port of the old
bot.py + utils/* package. The old ``utils`` package (Config/TOML
loader, LangChain-based ``ToolCallbackHandler``, ``ChatHistoryManager``,
telemetry, etc.) doesn't exist anymore — that machinery lived
server-side pre-v2 and the daemon owns it now. Like ``cli.py``, this
file talks to the server purely through ``scufris_client`` and reads
its own settings straight from a few env vars; no Config dataclass,
no TOML, no ``utils`` import.

Dropped vs the old bot.py (matches cli.py's v2 simplifications):
  * ``telemetry.begin_turn`` — that was LangChain-agent-runtime
    plumbing; cli.py doesn't use it either.
  * ``is_sub_agent`` / "asks" vs "uses" verb split — depth is always
    0 in v2 (single-agent post-swap), so the sub-agent distinction is
    gone. Every tool call renders as "X uses Y".
  * ``/stats`` — the v2 SDK has no stats endpoint (see TASK.md).
  * ``client.resolve_identity`` before ``chat_stream`` — chat_stream
    is channel-based now and resolves the user server-side. Identity
    resolution is only needed for ``/clear``, which still takes an
    int ``user_id``.
  * ``SCUFRIS_TOKEN`` / bearer auth — the v2 ``ScufrisClient`` has no
    ``token`` parameter (unauth-only per ADR-8); flagged, not silently
    dropped.

Connection settings (env vars):
  SCUFRIS_SERVER_URL   Base URL of the daemon (default http://127.0.0.1:7080).

Telegram settings (env vars, both required):
  TELEGRAM_BOT_TOKEN    Bot API token from @BotFather.
  ALLOWED_USER_IDS      Comma-separated list of Telegram user ids
                         permitted to use the bot.

The bot uses each Telegram user's numeric id as the server-side
``surface_id`` on the ``"telegram"`` surface.

Run with:  uv run scufris-bot   (the daemon must already be running)
"""

from __future__ import annotations

import logging
import os
import time
from functools import wraps
from typing import Callable, Optional

import dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from scufris_client import (
    ScufrisAuthError,
    ScufrisClient,
    ScufrisConnectionError,
    ScufrisError,
    ScufrisServerError,
    ThinkingEvent,
)

# ---------------------------------------------------------------------------
# Settings (env vars only — no Config/TOML in v2, mirrors cli.py)
# ---------------------------------------------------------------------------

SCUFRIS_SERVER_URL = os.environ.get("SCUFRIS_SERVER_URL", "http://127.0.0.1:7080")

# opencode agent the bot always talks to. Matches the CLI's hardcoded
# choice (v2 design decision D3); agent switching from Telegram is
# out of scope for this port.
SCUFRIS_AGENT = "build"

# Telegram caps message edits at ~30/sec per chat in practice. We
# rate-limit placeholder edits so a chatty agent run doesn't get us
# throttled or rate-limited.
PLACEHOLDER_EDIT_INTERVAL = 1.0  # seconds between consecutive edits
PLACEHOLDER_MAX_LEN = 3500  # leave headroom under Telegram's 4096 cap

# Telegram hard cap on a single message body. We truncate the expanded
# trace to fit answer + separator + trace.
TELEGRAM_MAX_MESSAGE = 4096

# Inline-keyboard labels for the collapsible thinking trace under
# every final answer. The arrow direction matches the action: ▼ means
# "expand downward", ▲ means "collapse upward".
THINKING_LABEL_SHOW = "💭 Show thinking ▼"
THINKING_LABEL_HIDE = "💭 Hide thinking ▲"

# Marker line that visually separates the answer from the (expanded)
# thinking trace. Italic + low-key on purpose.
_THINKING_SEPARATOR = "\n\n_— thinking —_\n"

# Per-message cache of (answer, thinking_trace), keyed by Telegram
# message_id of the answer. Bounded to keep memory predictable across
# a long-running bot. Survives only this bot process — after a restart,
# stale callbacks render a polite "(thinking trace expired)" notice.
_THINKING_CACHE_MAX = 256
_thinking_cache: "dict[int, tuple[str, str]]" = {}


def _store_thinking(message_id: int, answer: str, trace: str) -> None:
    """Cache ``(answer, trace)`` for a posted final-answer message.

    Evicts the oldest entry once :data:`_THINKING_CACHE_MAX` is exceeded.
    Insertion order is preserved by ``dict`` so eviction is FIFO.
    """
    _thinking_cache[message_id] = (answer, trace)
    while len(_thinking_cache) > _THINKING_CACHE_MAX:
        oldest = next(iter(_thinking_cache))
        del _thinking_cache[oldest]


def _thinking_keyboard(expanded: bool) -> InlineKeyboardMarkup:
    """Build the inline keyboard for the answer-message toggle.

    ``callback_data`` encodes the *desired* state on press, so the
    handler can be stateless: ``think:show`` expands, ``think:hide``
    collapses.
    """
    if expanded:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(THINKING_LABEL_HIDE, callback_data="think:hide")]]
        )
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(THINKING_LABEL_SHOW, callback_data="think:show")]]
    )


# Single client reused for every turn; opened in main() before polling
# starts and closed on shutdown.
_client: Optional[ScufrisClient] = None


def _monotonic() -> float:
    """Indirection so tests can fake the clock without monkey-patching
    ``time.monotonic`` globally (which breaks the event loop)."""
    return time.monotonic()


def _client_or_raise() -> ScufrisClient:
    if _client is None:
        raise RuntimeError("ScufrisClient not initialised — main() didn't run")
    return _client


# ---------------------------------------------------------------------------
# Tiny helpers (self-contained — no utils package in v2)
# ---------------------------------------------------------------------------


def truncate_log(text: str, max_length: int = 200) -> str:
    """Truncate text for logging, appending a length note when clipped."""
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}... ({len(text)} chars total)"


def display_name(raw: str) -> str:
    """Convert snake_case / dot.separated tool names to Title Case words."""
    return " ".join(w.capitalize() for w in raw.replace(".", "_").split("_"))


# Per-tool icon for the user-facing thinking trace. Falls back to a
# neutral wrench when a tool isn't listed. v2 NOTE: the old
# ``is_sub_agent`` verb split ("asks" vs "uses") is gone — depth is
# always 0 in v2 (see module docstring) — so every tool call renders
# the same way regardless of whether it's a leaf tool or a delegated
# agent on the server side.
_TOOL_ICONS: dict[str, str] = {
    "web_search": "🔍",
    "weather": "🌤",
    "calculator_tool": "🧮",
    "datetime_tool": "🕒",
    "opencode": "💻",
}


def _tool_icon(tool_name: str) -> str:
    return _TOOL_ICONS.get(tool_name, "🔧")


def setup_logging() -> logging.Logger:
    """Minimal logging setup: scufris-bot at INFO (or $LOG_LEVEL), everything else at ERROR."""
    level_str = os.environ.get("LOG_LEVEL")
    level = getattr(logging, level_str.upper(), logging.INFO) if level_str else logging.INFO

    logging.basicConfig(
        level=logging.ERROR,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("scufris-bot")
    logger.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.ERROR)
    logging.getLogger("httpcore").setLevel(logging.ERROR)
    logging.getLogger("telegram").setLevel(logging.ERROR)
    return logger


def _load_telegram_settings() -> tuple[str, list[int]]:
    """Read + validate the two required Telegram env vars.

    Raises ``SystemExit(1)`` (via a clear message, not a traceback) when
    either is missing — same hard-fail contract the old
    ``load_config(require_telegram=True)`` had.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        dotenv.load_dotenv()

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN not set — export it before starting the bot."
        )
    raw_ids = os.environ.get("ALLOWED_USER_IDS", "")
    allowed_ids: list[int] = []
    for tok in raw_ids.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            allowed_ids.append(int(tok))
        except ValueError:
            raise SystemExit(
                f"ALLOWED_USER_IDS: cannot parse {tok!r} as an integer in {raw_ids!r}"
            )
    if not allowed_ids:
        raise SystemExit(
            "ALLOWED_USER_IDS is empty — export at least one Telegram user id."
        )
    return bot_token, allowed_ids


logger = setup_logging()
TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS = _load_telegram_settings()


# ---------------------------------------------------------------------------
# Auth + small Telegram helpers (inlined — replaces utils.telegram)
# ---------------------------------------------------------------------------


def restricted(allowed_ids: list[int]) -> Callable:
    """Decorator factory restricting a handler to ``allowed_ids`` only."""

    def decorator(func):
        @wraps(func)
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
            user = update.effective_user
            assert user is not None, "Update has no effective_user"
            if user.id not in allowed_ids:
                logger.warning(
                    "unauthorized access attempt from %s (id=%s)",
                    user.username or user.first_name or "unknown",
                    user.id,
                )
                assert update.message is not None
                await update.message.reply_text("⛔ You are not authorized to use this bot.")
                return
            return await func(update, context, *a, **kw)

        return wrapped

    return decorator


def _get_message_text(update: Update) -> str:
    assert update.message is not None, "Update has no message"
    return update.message.text or ""


async def _send_error(update: Update, error: str) -> None:
    assert update.message is not None
    await update.message.reply_text(f"❌ Error: {error}")


async def _send_chunked(update: Update, text: str) -> None:
    """Send a long message, splitting into Telegram-sized chunks."""
    assert update.message is not None
    limit = 4000
    if len(text) <= limit:
        await update.message.reply_text(text)
        return
    for i in range(0, len(text), limit):
        chunk = text[i : i + limit]
        if i == 0:
            await update.message.reply_text(chunk)
        else:
            await update.message.chat.send_message(chunk)


# ---------------------------------------------------------------------------
# Per-process identity cache (only used by /clear — see module docstring)
# ---------------------------------------------------------------------------

_tg_id_cache: dict[int, int] = {}


async def _resolve_user_id(client: ScufrisClient, tg_id: int) -> int:
    """Return the server-side ``user_id`` for a Telegram numeric id.

    Calls ``POST /v1/identity/resolve`` once per Telegram id and caches
    the result for the lifetime of this process. On error we fall back
    to the raw Telegram id so the bot stays usable.

    Only needed for ``/clear`` — ``chat_stream`` resolves identity
    server-side from ``(surface, surface_id, agent)`` directly.
    """
    cached = _tg_id_cache.get(tg_id)
    if cached is not None:
        return cached
    try:
        body = await client.resolve_identity("telegram", str(tg_id))
        user_id = int(body["user_id"])
    except ScufrisError as exc:
        logger.warning(
            "identity resolve failed for telegram:%s (%s); using raw id", tg_id, exc
        )
        user_id = tg_id
    _tg_id_cache[tg_id] = user_id
    return user_id


# ----------------------------------------------------------------------
# Streaming → placeholder rendering
# ----------------------------------------------------------------------


class PlaceholderRenderer:
    """Accumulates streaming thinking events into a single Telegram message.

    The bot posts one "🤔 Thinking…" message at the start of a turn and
    edits it as new events arrive. On the final ``done`` event the
    placeholder is deleted (the actual answer is sent as a fresh
    message so it's clearly the agent's reply, not a status line).

    Edits are rate-limited and the body is hard-capped to keep us inside
    Telegram's 4096-char message limit even on chatty agent runs.
    """

    def __init__(self, message: Message):
        self._message = message
        self._lines: list[str] = []
        self._last_edit = 0.0
        self._pending = False
        self._closed = False

    def add(self, ev: ThinkingEvent) -> None:
        line = self._format(ev)
        if line is None:
            return
        self._lines.append(line)
        self._pending = True

    @staticmethod
    def _format(ev: ThinkingEvent) -> Optional[str]:
        indent = "  " * ev.depth
        branch = "└─ " if ev.depth > 0 else ""
        src = display_name(ev.source)
        if ev.kind == "tool_call":
            target = display_name(ev.text)
            icon = _tool_icon(ev.text)
            line = f"{indent}{branch}{icon} {src} uses {target}"
            if ev.arg:
                line += f": {ev.arg}"
            return line
        if ev.kind == "tool_meta":
            if ev.prior_turns and ev.prior_turns > 0:
                return f"{indent}  ↳ +{ev.prior_turns} prior turns"
            return None
        if ev.kind == "compaction":
            n_msg = ev.evicted or 0
            n_facts = ev.new_facts or 0
            return f"🧹 [memory] {ev.source}: compacted {n_msg} msg(s), +{n_facts} fact(s)"
        if ev.kind == "text":
            text = ev.text.replace("\n", " ")
            return f"{indent}  💭 {src}: {text}"
        # tool_result and unknown kinds — keep a short note for parity
        text = ev.text.replace("\n", " ")
        return f"{indent}  ↩ {text}"

    def _render(self) -> str:
        body = "\n".join(self._lines).strip() or "thinking…"
        # Cap to Telegram limit, keeping the *tail* (most recent activity).
        if len(body) > PLACEHOLDER_MAX_LEN:
            body = "…\n" + body[-(PLACEHOLDER_MAX_LEN - 2) :]
        return f"🤔 {body}"

    async def maybe_flush(self, *, force: bool = False) -> None:
        if self._closed or not self._pending:
            return
        now = _monotonic()
        if not force and (now - self._last_edit) < PLACEHOLDER_EDIT_INTERVAL:
            return
        text = self._render()
        try:
            await self._message.edit_text(text)
        except BadRequest as exc:
            # "Message is not modified" is benign; anything else we log
            # at debug — the placeholder is best-effort.
            if "not modified" in str(exc).lower():
                self._pending = False
                self._last_edit = now
                return
            logger.debug(f"placeholder edit failed: {exc}")
        except TelegramError as exc:
            logger.debug(f"placeholder edit failed: {exc}")
        else:
            self._last_edit = now
            self._pending = False

    async def close(self) -> None:
        """Delete the placeholder. Best-effort; failures are logged at debug."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._message.delete()
        except TelegramError as exc:
            logger.debug(f"placeholder delete failed: {exc}")

    def trace_text(self) -> str:
        """Return the accumulated thinking trace as plain text.

        Used to seed the collapsible-thinking cache so the answer
        message can re-render the trace on demand without re-running
        the agent.
        """
        return "\n".join(self._lines).strip()


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------


@restricted(ALLOWED_USER_IDS)
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a regular chat message by streaming the server's response."""
    request_start = time.time()

    user_message = _get_message_text(update)
    if not user_message:
        return

    user = update.effective_user
    assert user is not None
    tg_id = user.id
    username = user.username or user.first_name or "Unknown"

    logger.info(f"User {username} (ID:{tg_id}): {truncate_log(user_message, 100)}")

    assert update.message is not None, "Update has no message"
    client = _client_or_raise()

    # v2 NOTE: chat_stream is channel-based — it takes the raw Telegram
    # id as surface_id and resolves the user server-side, so no
    # separate resolve_identity() round-trip is needed here (unlike
    # /clear below, which still wants an int user_id).
    surface = "telegram"
    surface_id = str(tg_id)

    # Fire a typing action and post a placeholder we can edit.
    await update.message.chat.send_action("typing")
    placeholder = await update.message.reply_text("🤔 thinking…")
    renderer = PlaceholderRenderer(placeholder)

    final_text: Optional[str] = None
    error_text: Optional[str] = None

    try:
        async for stream_ev in client.chat_stream(
            surface, surface_id, SCUFRIS_AGENT, user_message
        ):
            if stream_ev.kind == "thinking" and stream_ev.thinking is not None:
                renderer.add(stream_ev.thinking)
                await renderer.maybe_flush()
            elif stream_ev.kind == "done":
                final_text = stream_ev.text or ""
                break
            elif stream_ev.kind == "error":
                error_text = stream_ev.error or "unknown error"
                break
    except ScufrisConnectionError as exc:
        await renderer.close()
        await _send_error(
            update,
            f"server unreachable: {exc}\n"
            "(is `scufris-server` running and reachable at "
            f"{SCUFRIS_SERVER_URL}?)",
        )
        return
    except ScufrisAuthError as exc:
        await renderer.close()
        await _send_error(update, f"auth failed: {exc}")
        return
    except ScufrisServerError as exc:
        await renderer.close()
        await _send_error(update, f"server error: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.error(
            f"Error processing request for telegram:{tg_id}: {exc}", exc_info=True
        )
        await renderer.close()
        await _send_error(update, f"getting response from AI:\n{exc}")
        return

    await renderer.close()

    if error_text is not None:
        await _send_error(update, error_text)
        return
    if final_text is None:
        await _send_error(update, "stream ended without a final response")
        return

    send_start = time.time()
    trace = renderer.trace_text()
    if trace:
        # Post the answer as a single message with a "Show thinking"
        # toggle. We post directly (bypassing chunking) because inline
        # keyboards need a single message to attach to. If the answer
        # itself is too long, fall back to plain chunked send and skip
        # the toggle — better than truncating the user's reply.
        if len(final_text) <= TELEGRAM_MAX_MESSAGE:
            sent = await update.message.reply_text(
                final_text, reply_markup=_thinking_keyboard(expanded=False)
            )
            _store_thinking(sent.message_id, final_text, trace)
        else:
            logger.debug(
                "answer too long for inline-keyboard toggle (%d chars); "
                "falling back to chunked send",
                len(final_text),
            )
            await _send_chunked(update, final_text)
    else:
        await _send_chunked(update, final_text)
    send_duration = time.time() - send_start
    total_duration = time.time() - request_start

    logger.info(
        f"Request completed | total={total_duration:.2f}s "
        f"(send={send_duration:.2f}s) | response={len(final_text)} chars"
    )


async def thinking_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the inline-keyboard toggle on a final-answer message.

    ``callback_data`` is ``think:show`` (expand) or ``think:hide``
    (collapse). When the cache is missing the trace (e.g. the bot was
    restarted between answer and tap) we politely say so and remove
    the keyboard so the button can't be pressed again.
    """
    query = update.callback_query
    if query is None or query.data is None or query.message is None:
        return
    await query.answer()

    parts = query.data.split(":", 1)
    if len(parts) != 2 or parts[0] != "think":
        return
    target = parts[1]  # "show" | "hide"

    msg_id = query.message.message_id
    cached = _thinking_cache.get(msg_id)
    if cached is None:
        # Stale (post-restart) — drop the keyboard and tell the user.
        prior = getattr(query.message, "text", None) or ""
        try:
            await query.edit_message_text(
                prior + "\n\n_(thinking trace expired)_",
                parse_mode="Markdown",
            )
        except TelegramError as exc:
            logger.debug("stale-trace edit failed: %s", exc)
        return

    answer, trace = cached
    if target == "show":
        # Truncate trace if needed so answer + separator + trace fits.
        budget = TELEGRAM_MAX_MESSAGE - len(answer) - len(_THINKING_SEPARATOR) - 16
        body = trace if len(trace) <= max(budget, 0) else "…\n" + trace[-budget:]
        new_text = answer + _THINKING_SEPARATOR + body
        keyboard = _thinking_keyboard(expanded=True)
    else:
        new_text = answer
        keyboard = _thinking_keyboard(expanded=False)

    try:
        await query.edit_message_text(
            new_text, parse_mode="Markdown", reply_markup=keyboard
        )
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        # Markdown can blow up on unbalanced underscores in the trace —
        # retry without parse_mode rather than failing the toggle.
        logger.debug("toggle edit (markdown) failed: %s — retrying plain", exc)
        try:
            await query.edit_message_text(new_text, reply_markup=keyboard)
        except TelegramError as exc2:
            logger.debug("toggle edit (plain) failed: %s", exc2)
    except TelegramError as exc:
        logger.debug("toggle edit failed: %s", exc)


@restricted(ALLOWED_USER_IDS)
async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear chat history for the user (delegates to the server).

    v2 NOTE: ``client.clear(user_id)`` still takes a resolved integer
    ``user_id``, unlike ``chat_stream``, so identity resolution is
    still needed here. The response shape is just ``{"count": int}``
    — the per-agent breakdown that v1 returned is gone.
    """
    user = update.effective_user
    assert user is not None
    tg_id = user.id
    username = user.username or user.first_name or "Unknown"
    assert update.message is not None, "Update has no message"
    client = _client_or_raise()
    user_id = await _resolve_user_id(client, tg_id)

    try:
        result = await client.clear(user_id)
    except ScufrisError as exc:
        await _send_error(update, f"clear failed: {exc}")
        return

    count = int(result.get("count", 0))

    logger.info(f"Cleared {count} session link(s) for user {username} (ID:{tg_id})")

    if count == 0:
        msg = "🗑️ No sessions to clear."
    elif count == 1:
        msg = "🗑️ Cleared 1 session."
    else:
        msg = f"🗑️ Cleared {count} sessions."

    await update.message.reply_text(msg)


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------


async def _post_init(app) -> None:
    """Build the HTTP client and probe the server, in the bot's own loop.

    The :class:`ScufrisClient` (really its ``httpx.AsyncClient`` pool)
    binds itself to whichever event loop it first does I/O on. Doing
    that probe via ``asyncio.run`` from the synchronous ``main`` would
    pin the pool to a *different*, immediately-closed loop, which then
    blows up the moment the polling loop tries to reuse a connection
    (``RuntimeError: Event loop is closed``). Building + probing here
    keeps the client and the polling loop in lock-step.

    Failing this hook aborts ``app.run_polling`` before any updates are
    fetched, satisfying the "no silent partial bring-up" contract.
    """
    global _client
    # v2 NOTE: ScufrisClient currently has no `token` parameter — the
    # SDK is unauth-only per ADR-8 (loopback trust model). There's no
    # SCUFRIS_TOKEN to pass here at all; flagging this for review since
    # it's a behavior change from bot.py, which did send a bearer token.
    _client = ScufrisClient(base_url=SCUFRIS_SERVER_URL)
    try:
        await _client.healthz()
    except ScufrisConnectionError as exc:
        logger.critical(
            f"scufris-server unreachable at {SCUFRIS_SERVER_URL}: {exc}\n"
            "Start the server (e.g. `uv run scufris-server`) and retry."
        )
        await _client.aclose()
        _client = None
        raise SystemExit(1) from exc
    except ScufrisAuthError as exc:
        logger.critical(f"scufris-server auth failed: {exc}")
        await _client.aclose()
        _client = None
        raise SystemExit(1) from exc
    except ScufrisError as exc:
        logger.critical(f"scufris-server health check failed: {exc}")
        await _client.aclose()
        _client = None
        raise SystemExit(1) from exc

    logger.info("Server reachable; bot is now ready.")


async def _post_shutdown(_app) -> None:
    if _client is not None:
        await _client.aclose()


def main() -> None:
    logger.info("Starting Scufris Bot...")
    logger.info(f"Will connect to scufris-server at {SCUFRIS_SERVER_URL}")

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    logger.info("Registering command handlers")
    app.add_handler(CommandHandler("clear", clear_history))

    logger.info("Registering message handlers")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_handler(CallbackQueryHandler(thinking_toggle, pattern=r"^think:"))

    logger.info("Starting polling (server health is probed in post_init)...")
    app.run_polling()


if __name__ == "__main__":
    main()

"""Telegram front-end for the Scufris agent — HTTP client of ``scufris-server``.

The bot's brain (agent runtime, history, tools) lives in the daemon; this
module is just the Telegram UX. Stopping and restarting the bot doesn't
evict anyone's conversation — that lives in the daemon.

Connection settings:
  * ``SCUFRIS_SERVER_URL`` — base URL of the daemon
    (default ``http://127.0.0.1:8765``).
  * ``SCUFRIS_TOKEN`` — bearer token, only required when the server is
    configured with one. Shared with ``scufris-cli``.

Telegram settings (still required, same as before):
  * ``TELEGRAM_BOT_TOKEN`` — Bot API token from @BotFather.
  * ``ALLOWED_USER_IDS`` — comma-separated list of Telegram user ids
    permitted to use the bot.

The bot uses each Telegram user's numeric id as the server ``user_id``,
so per-user history is preserved across bot restarts and shareable with
``scufris-cli`` if you set ``SCUFRIS_USER_ID`` to your Telegram id.

Run with:  uv run scufris-bot   (the daemon must already be running)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

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
)
from utils import (
    TelegramTransport,
    ThinkingEvent,
    display_name,
    is_sub_agent,
    load_config,
    restricted,
    setup_logging,
    truncate_log,
)
from utils.telemetry import begin_turn

logger = setup_logging(default_level=logging.INFO)
config = load_config(require_telegram=True)
telegram_transport = TelegramTransport(list(config.telegram.allowed_user_ids))

SCUFRIS_SERVER_URL = config.client.server_url
SCUFRIS_TOKEN = config.server.token

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


# Per-tool icon for the user-facing thinking trace. Sub-agents share a
# single icon (🤖) so delegations are visually distinct from leaf-tool
# calls. Falls back to a neutral wrench when a tool isn't listed.
_TOOL_ICONS: dict[str, str] = {
    "web_search": "🔍",
    "weather": "🌤",
    "calculator_tool": "🧮",
    "datetime_tool": "🕒",
    "opencode": "💻",
}


def _tool_icon(tool_name: str) -> str:
    if is_sub_agent(tool_name):
        return "🤖"
    return _TOOL_ICONS.get(tool_name, "🔧")


def _client_or_raise() -> ScufrisClient:
    if _client is None:
        raise RuntimeError("ScufrisClient not initialised — main() didn't run")
    return _client


# Per-process cache of Telegram-id → server user_id, populated lazily by
# :func:`_resolve_user_id`. Restarting the bot empties it, which is fine:
# resolution is idempotent and the next message just calls the server
# again. Bounded only by the number of distinct allowed Telegram users,
# so a plain dict is enough.
_tg_id_cache: dict[int, int] = {}


async def _resolve_user_id(client: ScufrisClient, tg_id: int) -> int:
    """Return the server-side ``user_id`` for a Telegram numeric id.

    Calls ``POST /v1/identity/resolve`` once per Telegram id and caches
    the result for the lifetime of this process. On error (e.g. talking
    to an older server without the endpoint) we fall back to the raw
    Telegram id — the legacy wire shape — so the bot stays usable.
    """
    cached = _tg_id_cache.get(tg_id)
    if cached is not None:
        return cached
    try:
        body = await client.resolve_identity("telegram", str(tg_id))
        user_id = int(body["user_id"])
    except ScufrisError as exc:
        logger.warning(
            "identity resolve failed for telegram:%s (%s); using raw id",
            tg_id,
            exc,
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
            verb = "asks" if is_sub_agent(ev.text) else "uses"
            line = f"{indent}{branch}{icon} {src} {verb} {target}"
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
            return (
                f"🧹 [memory] {ev.source}: compacted {n_msg} msg(s), +{n_facts} fact(s)"
            )
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


@restricted(list(config.telegram.allowed_user_ids))
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a regular chat message by streaming the server's response."""
    request_start = time.time()

    user_message = telegram_transport.get_message_text(update)
    if not user_message:
        return

    user_info = telegram_transport.get_user_info(update)
    tg_id = user_info["id"]

    logger.info(
        f"User {user_info['username']} (ID:{tg_id}): {truncate_log(user_message, 100)}"
    )

    assert update.message is not None, "Update has no message"
    client = _client_or_raise()
    user_id = await _resolve_user_id(client, tg_id)

    # Fire a typing action and post a placeholder we can edit.
    await telegram_transport.send_typing_action(update)
    placeholder = await update.message.reply_text("🤔 thinking…")
    renderer = PlaceholderRenderer(placeholder)

    final_text: Optional[str] = None
    error_text: Optional[str] = None

    try:
        with begin_turn(f"telegram:{tg_id}"):
            async for ev in client.chat_stream(user_id, user_message):
                if ev.kind == "thinking" and ev.thinking is not None:
                    renderer.add(ev.thinking)
                    await renderer.maybe_flush()
                elif ev.kind == "done":
                    final_text = ev.text or ""
                    break
                elif ev.kind == "error":
                    error_text = ev.error or "unknown error"
                    break
    except ScufrisConnectionError as exc:
        await renderer.close()
        await telegram_transport.send_error_message(
            update,
            f"server unreachable: {exc}\n"
            "(is `scufris-server` running and reachable at "
            f"{SCUFRIS_SERVER_URL}?)",
        )
        return
    except ScufrisAuthError as exc:
        await renderer.close()
        await telegram_transport.send_error_message(
            update, f"auth failed: {exc} (check $SCUFRIS_TOKEN)"
        )
        return
    except ScufrisServerError as exc:
        await renderer.close()
        await telegram_transport.send_error_message(update, f"server error: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.error(
            f"Error processing request for user {user_id}: {exc}", exc_info=True
        )
        await renderer.close()
        await telegram_transport.send_error_message(
            update, f"getting response from AI:\n{exc}"
        )
        return

    await renderer.close()

    if error_text is not None:
        await telegram_transport.send_error_message(update, error_text)
        return
    if final_text is None:
        await telegram_transport.send_error_message(
            update, "stream ended without a final response"
        )
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
            await telegram_transport.send_message(update, final_text)
    else:
        await telegram_transport.send_message(update, final_text)
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
        # ``query.message`` is typed ``MaybeInaccessibleMessage``; fall
        # back to "" when ``.text`` is unavailable.
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


@restricted(list(config.telegram.allowed_user_ids))
async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear chat history for the user (delegates to the server)."""
    user_info = telegram_transport.get_user_info(update)
    tg_id = user_info["id"]
    assert update.message is not None, "Update has no message"
    client = _client_or_raise()
    user_id = await _resolve_user_id(client, tg_id)

    try:
        result = await client.clear(user_id)
    except ScufrisError as exc:
        await telegram_transport.send_error_message(update, f"clear failed: {exc}")
        return

    cleared = int(result.get("cleared", 0))
    breakdown = result.get("breakdown") or {}

    logger.info(
        f"Cleared {cleared} messages for user {user_info['username']} (ID:{tg_id})"
    )

    if cleared == 0:
        msg = "🗑️ No messages to clear."
    elif breakdown:
        parts = ", ".join(f"{a}: {n}" for a, n in sorted(breakdown.items()))
        msg = f"🗑️ Cleared {cleared} messages ({parts})."
    else:
        msg = f"🗑️ Cleared {cleared} messages from your chat history."

    await update.message.reply_text(msg)


@restricted(list(config.telegram.allowed_user_ids))
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show per-agent memory + telemetry breakdown (delegates to the server)."""
    user_info = telegram_transport.get_user_info(update)
    tg_id = user_info["id"]
    assert update.message is not None, "Update has no message"
    client = _client_or_raise()
    user_id = await _resolve_user_id(client, tg_id)

    try:
        result = await client.stats(user_id)
    except ScufrisError as exc:
        await telegram_transport.send_error_message(update, f"stats failed: {exc}")
        return

    text = format_telegram_stats(result)
    await update.message.reply_text(text, parse_mode="Markdown")


def _md_escape(text: str) -> str:
    """Escape characters that have meaning in Telegram legacy Markdown.

    Legacy Markdown only treats ``_ * ` [`` specially outside code blocks.
    We use this for inline values like model names that may contain
    underscores (e.g. ``qwen2_5:14b``).
    """
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def format_telegram_stats(payload: dict) -> str:
    """Format ``/stats`` payload for Telegram (legacy Markdown).

    Layout:
      bold heading + summary scalars (inline-code values),
      ``Per-agent:`` bold + monospace fenced table,
      ``Tool usage:`` bold + monospace fenced histogram.

    Values that contain underscores or backticks are escaped so they
    don't accidentally start italic/code spans. Tables stay inside
    fenced code blocks where Markdown is inert, so column alignment
    is preserved verbatim.
    """
    summary = payload.get("summary") or {}
    rows = payload.get("rows") or {}
    tools = payload.get("tools") or {}

    out: list[str] = ["*📊 Scufris session*", ""]
    if summary:
        uptime = _md_escape(str(summary.get("uptime", "—")))
        model = _md_escape(str(summary.get("model", "—")))
        base_url = _md_escape(str(summary.get("base_url", "—")))
        total_msgs = summary.get("total_messages", 0)
        total_inv = summary.get("total_invocations", 0)
        out.append(f"_Uptime:_ `{uptime}`")
        out.append(f"_Model:_ `{model}` @ `{base_url}`")
        out.append(f"_Messages:_ *{total_msgs}*  _Invocations:_ *{total_inv}*")
        out.append("")

    # Per-agent table — render inside a fenced code block. We re-use the
    # same column logic as format_stats_lines but operate directly on
    # ``rows`` so we don't depend on parsing the daemon's pre-rendered
    # text.
    if rows:
        from utils.stats import format_relative

        def _coerce_ts(value):
            """Last-activity comes back as an ISO string over JSON."""
            if value is None or isinstance(value, datetime):
                return value
            if isinstance(value, str):
                try:
                    # Python's fromisoformat accepts the trailing Z only
                    # from 3.11+, but we still defensively normalise it.
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    return None
            return None

        ordered = sorted(
            rows.items(),
            key=lambda kv: (kv[1].get("history_disabled", False), kv[0]),
        )
        header = ("agent", "model", "memory", "calls", "last")
        table_rows: list[tuple] = [header]
        for agent, t in ordered:
            model_cell = t.get("model") or "—"
            calls_cell = str(t.get("invocations", 0))
            last_cell = format_relative(_coerce_ts(t.get("last_activity")))
            if t.get("history_disabled"):
                memory_cell = "(history disabled)"
            elif not t.get("messages"):
                memory_cell = "0 msgs"
            else:
                msgs = t["messages"]
                tokens = t.get("tokens", 0)
                budget = t.get("budget")
                if budget:
                    pct = (tokens * 100) // budget
                    memory_cell = f"{msgs} msgs / ~{tokens} tok ({pct}%)"
                else:
                    memory_cell = f"{msgs} msgs / ~{tokens} tok"
            table_rows.append((agent, model_cell, memory_cell, calls_cell, last_cell))

        widths = [max(len(r[i]) for r in table_rows) for i in range(len(header))]

        def _fmt(r: tuple) -> str:
            return (
                f"{r[0]:<{widths[0]}}  {r[1]:<{widths[1]}}  "
                f"{r[2]:<{widths[2]}}  {r[3]:>{widths[3]}}  {r[4]:<{widths[4]}}"
            ).rstrip()

        out.append("*Per-agent:*")
        out.append("```")
        out.append(_fmt(header))
        out.append("  ".join("─" * w for w in widths))
        for r in table_rows[1:]:
            out.append(_fmt(r))
        out.append("```")
        out.append("")

    # Tool histogram.
    if tools:
        items = sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        max_count = items[0][1]
        name_width = max(len(name) for name, _ in items)
        bar_width = 8
        out.append("*Tool usage:*")
        out.append("```")
        for name, count in items:
            bar_len = max(1, (count * bar_width + max_count - 1) // max_count)
            bar = "█" * bar_len
            out.append(f"{name:<{name_width}}  {bar:<{bar_width}}  {count}")
        out.append("```")

    return "\n".join(out).rstrip()


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
    _client = ScufrisClient(base_url=SCUFRIS_SERVER_URL, token=SCUFRIS_TOKEN)
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
        logger.critical(
            f"scufris-server auth failed: {exc}\n"
            "Check $SCUFRIS_TOKEN matches the server's configuration."
        )
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

    # require_telegram=True (the default in load_config) guarantees this.
    assert config.telegram.bot_token is not None
    app = (
        ApplicationBuilder()
        .token(config.telegram.bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    logger.info("Registering command handlers")
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CommandHandler("stats", stats_command))

    logger.info("Registering message handlers")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_handler(CallbackQueryHandler(thinking_toggle, pattern=r"^think:"))

    logger.info("Starting polling (server health is probed in post_init)...")
    app.run_polling()


if __name__ == "__main__":
    main()

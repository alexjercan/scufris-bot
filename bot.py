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
from typing import Optional

from telegram import Message, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
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
        src = display_name(ev.source)
        if ev.kind == "tool_call":
            target = display_name(ev.text)
            verb = "asks" if is_sub_agent(ev.text) else "uses"
            line = f"{indent}→ {src} {verb} {target}"
            if ev.arg:
                line += f": {truncate_log(ev.arg, 80)}"
            return line
        if ev.kind == "tool_meta":
            if ev.prior_turns and ev.prior_turns > 0:
                return f"{indent}  ↳ +{ev.prior_turns} prior turns"
            return None
        if ev.kind == "compaction":
            n_msg = ev.evicted or 0
            n_facts = ev.new_facts or 0
            return f"[memory] {ev.source}: compacted {n_msg} msg(s), +{n_facts} fact(s)"
        if ev.kind == "text":
            text = truncate_log(ev.text.replace("\n", " "), 200)
            return f"{indent}{src}: {text}"
        # tool_result and unknown kinds — keep a short note for parity
        text = truncate_log(ev.text.replace("\n", " "), 200)
        return f"{indent}↩ {text}"

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
    await telegram_transport.send_message(update, final_text)
    send_duration = time.time() - send_start
    total_duration = time.time() - request_start

    logger.info(
        f"Request completed | total={total_duration:.2f}s "
        f"(send={send_duration:.2f}s) | response={len(final_text)} chars"
    )


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

    lines = result.get("lines") or []
    body = "\n".join(lines) if lines else "(no stats available)"
    # Wrap in a Markdown code fence for stable column alignment on Telegram.
    await update.message.reply_text(f"```\n{body}\n```", parse_mode="Markdown")


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

    logger.info("Starting polling (server health is probed in post_init)...")
    app.run_polling()


if __name__ == "__main__":
    main()

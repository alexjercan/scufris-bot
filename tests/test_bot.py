"""Tests for the Telegram bot front-end (``bot.py``).

The bot is now a thin HTTP client of ``scufris-server``. We test:

  * Per-event placeholder rendering (the visible "thinking..." trail).
  * Rate-limited message edits (we don't spam Telegram).
  * Streaming/done dispatch in the chat handler with a stubbed
    ``ScufrisClient`` and a hand-rolled Update/Message double — no real
    network or telegram traffic.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator, Iterator, List
from unittest.mock import AsyncMock, MagicMock

import pytest

# bot.py imports load_config() at import time which validates Telegram
# env vars. Set placeholders before the module is imported.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:fake")
os.environ.setdefault("ALLOWED_USER_IDS", "42")

import bot  # noqa: E402
from scufris_client import StreamEvent  # noqa: E402
from utils.callbacks import ThinkingEvent  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_identity_cache() -> Iterator[None]:
    """The bot caches Telegram-id → user_id resolutions for the lifetime
    of the process; tests poke ``bot._client`` with stub doubles, so
    leftover entries from a previous test would mask real resolve
    behavior. Reset before *and* after each test."""
    bot._tg_id_cache.clear()
    yield
    bot._tg_id_cache.clear()


# ----------------------------------------------------------------------
# PlaceholderRenderer formatting
# ----------------------------------------------------------------------


def _ev(**kw: Any) -> ThinkingEvent:
    return ThinkingEvent(
        kind=kw.get("kind", "text"),
        source=kw.get("source", "main"),
        text=kw.get("text", ""),
        depth=kw.get("depth", 0),
        arg=kw.get("arg"),
        context=kw.get("context"),
        prior_turns=kw.get("prior_turns"),
        evicted=kw.get("evicted"),
        new_facts=kw.get("new_facts"),
    )


def test_format_tool_call_uses_friendly_names() -> None:
    line = bot.PlaceholderRenderer._format(
        _ev(kind="tool_call", source="main", text="knowledge_agent", arg="weather")
    )
    assert line is not None
    assert "Scufris" in line
    assert "Knowledge Agent" in line
    assert "asks" in line  # sub-agent → asks (not uses)
    assert "weather" in line


def test_format_tool_call_leaf_tool_uses_verb_uses() -> None:
    line = bot.PlaceholderRenderer._format(
        _ev(kind="tool_call", source="knowledge_agent", text="web_search", arg="x")
    )
    assert line is not None
    assert "uses" in line
    assert "asks" not in line


def test_format_tool_meta_emits_only_for_positive_prior_turns() -> None:
    none_ev = bot.PlaceholderRenderer._format(
        _ev(kind="tool_meta", source="main", text="t", prior_turns=0)
    )
    assert none_ev is None
    line = bot.PlaceholderRenderer._format(
        _ev(kind="tool_meta", source="main", text="t", prior_turns=3, depth=1)
    )
    assert line is not None
    assert "+3 prior turns" in line


def test_format_compaction() -> None:
    line = bot.PlaceholderRenderer._format(
        _ev(kind="compaction", source="main", text="", evicted=2, new_facts=1)
    )
    assert line is not None
    assert "compacted 2 msg" in line
    assert "+1 fact" in line


def test_format_text_collapses_newlines() -> None:
    line = bot.PlaceholderRenderer._format(
        _ev(kind="text", source="main", text="hello\nworld", depth=2)
    )
    assert line is not None
    assert "\n" not in line.strip("\n")  # the single line itself has no \n
    assert "hello world" in line


# ----------------------------------------------------------------------
# PlaceholderRenderer rate limiting + flush + close
# ----------------------------------------------------------------------


def _make_message_double() -> MagicMock:
    msg = MagicMock()
    msg.edit_text = AsyncMock(return_value=None)
    msg.delete = AsyncMock(return_value=None)
    return msg


def test_rate_limited_edits_skip_until_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    msg = _make_message_double()
    renderer = bot.PlaceholderRenderer(msg)

    # Pretend almost no time has passed between events. The first flush
    # is allowed (last_edit=0 ⇒ now-last is huge), subsequent ones are
    # gated by PLACEHOLDER_EDIT_INTERVAL.
    times = iter([10.0, 10.1, 10.2, 11.5])
    monkeypatch.setattr(bot, "_monotonic", lambda: next(times))

    async def go() -> None:
        renderer.add(_ev(kind="text", source="main", text="one"))
        await renderer.maybe_flush()  # uses 10.0 → allowed
        renderer.add(_ev(kind="text", source="main", text="two"))
        await renderer.maybe_flush()  # uses 10.1 → blocked
        renderer.add(_ev(kind="text", source="main", text="three"))
        await renderer.maybe_flush()  # uses 10.2 → blocked
        renderer.add(_ev(kind="text", source="main", text="four"))
        await renderer.maybe_flush()  # uses 11.5 → allowed (>1s gap)

    asyncio.run(go())
    assert msg.edit_text.await_count == 2


def test_force_flush_bypasses_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    msg = _make_message_double()
    renderer = bot.PlaceholderRenderer(msg)
    monkeypatch.setattr(bot, "_monotonic", lambda: 100.0)

    async def go() -> None:
        renderer.add(_ev(kind="text", source="main", text="x"))
        await renderer.maybe_flush()  # allowed
        renderer.add(_ev(kind="text", source="main", text="y"))
        await renderer.maybe_flush(force=True)  # forced

    asyncio.run(go())
    assert msg.edit_text.await_count == 2


def test_close_deletes_placeholder() -> None:
    msg = _make_message_double()
    renderer = bot.PlaceholderRenderer(msg)
    asyncio.run(renderer.close())
    asyncio.run(renderer.close())  # idempotent
    assert msg.delete.await_count == 1


def test_render_caps_to_max_length(monkeypatch: pytest.MonkeyPatch) -> None:
    msg = _make_message_double()
    renderer = bot.PlaceholderRenderer(msg)
    # Stuff lots of long lines into the buffer.
    big = "X" * 200
    for _ in range(100):
        renderer.add(_ev(kind="text", source="main", text=big))
    rendered = renderer._render()
    # +2 for the leading "🤔 " and a possible "…\n" prefix, but should
    # definitely fit inside Telegram's 4096-char ceiling with margin.
    assert len(rendered) <= bot.PLACEHOLDER_MAX_LEN + 4


def test_edit_not_modified_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from telegram.error import BadRequest

    msg = _make_message_double()
    msg.edit_text.side_effect = BadRequest("Message is not modified")
    renderer = bot.PlaceholderRenderer(msg)
    monkeypatch.setattr(bot, "_monotonic", lambda: 0.0)

    async def go() -> None:
        renderer.add(_ev(kind="text", source="main", text="x"))
        # Should not raise.
        await renderer.maybe_flush()

    asyncio.run(go())


# ----------------------------------------------------------------------
# chat handler — streams from a stubbed client into a fake Update.
# ----------------------------------------------------------------------


class _StubClient:
    """Stand-in for ScufrisClient used by the chat handler tests."""

    def __init__(self, events: List[StreamEvent]):
        self._events = events
        self.calls: List[tuple[int, str]] = []
        self.identity_calls: List[tuple[str, str]] = []

    async def chat_stream(
        self, user_id: int, message: str
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append((user_id, message))
        for ev in self._events:
            yield ev

    async def resolve_identity(self, surface: str, surface_id: str) -> dict:
        # Mirror legacy behavior: numeric surface_id passes through as
        # the user_id so existing assertions keep working.
        self.identity_calls.append((surface, surface_id))
        try:
            uid = int(surface_id)
        except ValueError:
            uid = abs(hash(surface_id))
        return {
            "user_id": uid,
            "username": None,
            "surface": surface,
            "surface_id": surface_id,
            "bound_surfaces": [],
        }


def _make_update_double(user_id: int = 42, text: str = "hello") -> MagicMock:
    update = MagicMock()
    user = MagicMock()
    user.id = user_id
    user.username = "tester"
    user.first_name = "Test"
    user.last_name = None
    update.effective_user = user

    # The response message that reply_text returns (our placeholder).
    placeholder = _make_message_double()

    msg = MagicMock()
    msg.text = text
    msg.reply_text = AsyncMock(return_value=placeholder)
    msg.chat = MagicMock()
    msg.chat.send_action = AsyncMock(return_value=None)
    msg.chat.send_message = AsyncMock(return_value=None)

    update.message = msg
    update._placeholder = placeholder  # for test inspection
    return update


def test_chat_handler_streams_to_placeholder_and_sends_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: List[StreamEvent] = [
        StreamEvent(
            kind="thinking",
            thinking=_ev(kind="text", source="main", text="reasoning"),
        ),
        StreamEvent(kind="done", text="final answer"),
    ]
    stub = _StubClient(events)
    monkeypatch.setattr(bot, "_client", stub)

    update = _make_update_double(user_id=42, text="hello")
    asyncio.run(bot.chat.__wrapped__(update, MagicMock()))

    # Client was invoked with the Telegram user id verbatim.
    assert stub.calls == [(42, "hello")]

    # Placeholder was created, then deleted at the end.
    update.message.reply_text.assert_any_await("🤔 thinking…")
    update._placeholder.delete.assert_awaited()

    # Final answer was sent. send_long_message routes through
    # update.message.reply_text on the first chunk.
    sent = [
        c.args[0]
        for c in update.message.reply_text.await_args_list
        if c.args and c.args[0] != "🤔 thinking…"
    ]
    assert "final answer" in sent


def test_chat_handler_reports_server_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: List[StreamEvent] = [StreamEvent(kind="error", error="boom")]
    monkeypatch.setattr(bot, "_client", _StubClient(events))

    update = _make_update_double()
    asyncio.run(bot.chat.__wrapped__(update, MagicMock()))

    # The placeholder is closed and an error message is delivered.
    update._placeholder.delete.assert_awaited()
    sent_texts = [c.args[0] for c in update.message.reply_text.await_args_list]
    assert any("boom" in t for t in sent_texts if isinstance(t, str))


def test_chat_handler_handles_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scufris_client import ScufrisConnectionError

    class _BoomClient:
        async def chat_stream(self, user_id: int, message: str):
            raise ScufrisConnectionError("connection refused")
            yield  # pragma: no cover — make this an async generator

        async def resolve_identity(self, surface: str, surface_id: str) -> dict:
            return {
                "user_id": int(surface_id),
                "username": None,
                "surface": surface,
                "surface_id": surface_id,
                "bound_surfaces": [],
            }

    monkeypatch.setattr(bot, "_client", _BoomClient())
    update = _make_update_double()
    asyncio.run(bot.chat.__wrapped__(update, MagicMock()))

    sent_texts = [
        c.args[0]
        for c in update.message.reply_text.await_args_list
        if isinstance(c.args[0], str)
    ]
    assert any("server unreachable" in t for t in sent_texts)


# ----------------------------------------------------------------------
# /clear and /stats — delegate to the server via the client.
# ----------------------------------------------------------------------


class _CmdClient:
    def __init__(self, **payloads: Any):
        self._payloads = payloads
        self.cleared_for: List[int] = []
        self.stats_for: List[int] = []

    async def clear(self, user_id: int) -> dict:
        self.cleared_for.append(user_id)
        return self._payloads.get("clear", {"cleared": 0})

    async def stats(self, user_id: int) -> dict:
        self.stats_for.append(user_id)
        return self._payloads.get("stats", {"lines": []})

    async def resolve_identity(self, surface: str, surface_id: str) -> dict:
        return {
            "user_id": int(surface_id),
            "username": None,
            "surface": surface,
            "surface_id": surface_id,
            "bound_surfaces": [],
        }


def test_clear_handler_delegates_to_server(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _CmdClient(clear={"cleared": 3, "breakdown": {"main": 2, "knowledge": 1}})
    monkeypatch.setattr(bot, "_client", client)
    update = _make_update_double(user_id=42)

    asyncio.run(bot.clear_history.__wrapped__(update, MagicMock()))

    assert client.cleared_for == [42]
    sent = [c.args[0] for c in update.message.reply_text.await_args_list]
    assert any("Cleared 3 messages" in t for t in sent)


def test_clear_handler_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot, "_client", _CmdClient(clear={"cleared": 0}))
    update = _make_update_double(user_id=42)
    asyncio.run(bot.clear_history.__wrapped__(update, MagicMock()))
    sent = [c.args[0] for c in update.message.reply_text.await_args_list]
    assert any("No messages to clear" in t for t in sent)


def test_stats_handler_delegates_to_server(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _CmdClient(
        stats={
            "lines": ["Scufris session stats", "Uptime: 1m"],
            "rows": {},
            "tools": {},
            "summary": {
                "uptime": "1m 0s",
                "model": "qwen3",
                "base_url": "http://stub",
                "total_messages": 0,
                "total_invocations": 0,
            },
        }
    )
    monkeypatch.setattr(bot, "_client", client)
    update = _make_update_double(user_id=42)

    asyncio.run(bot.stats_command.__wrapped__(update, MagicMock()))

    assert client.stats_for == [42]
    sent = [c.args[0] for c in update.message.reply_text.await_args_list]
    # New format uses bold heading with emoji + Markdown formatting.
    assert any("Scufris session" in t for t in sent)
    # Markdown parse_mode used (rather than the old code-fence wrapping).
    kwargs_seen = [c.kwargs for c in update.message.reply_text.await_args_list]
    assert any(kw.get("parse_mode") == "Markdown" for kw in kwargs_seen)


# ----------------------------------------------------------------------
# format_telegram_stats — pure renderer
# ----------------------------------------------------------------------


def _stats_payload(**overrides: Any) -> dict:
    base = {
        "lines": [],
        "rows": {},
        "tools": {},
        "summary": {
            "uptime": "1h 23m",
            "model": "qwen3:14b",
            "base_url": "http://localhost:11434",
            "total_messages": 12,
            "total_invocations": 7,
        },
    }
    base.update(overrides)
    return base


def test_format_telegram_stats_includes_summary_scalars() -> None:
    text = bot.format_telegram_stats(_stats_payload())
    assert "📊 Scufris session" in text
    assert "1h 23m" in text
    # Model has a colon (no escaping needed) but underscores would be
    # escaped — check the renderer surfaces the value verbatim here.
    assert "qwen3:14b" in text
    assert "http://localhost:11434" in text


def test_format_telegram_stats_escapes_underscores_in_model_name() -> None:
    payload = _stats_payload(
        summary={
            "uptime": "1m",
            "model": "qwen2_5:14b",
            "base_url": "http://x",
            "total_messages": 0,
            "total_invocations": 0,
        }
    )
    text = bot.format_telegram_stats(payload)
    # Underscore should be escaped to avoid italic spans.
    assert "qwen2\\_5:14b" in text


def test_format_telegram_stats_renders_per_agent_table() -> None:
    payload = _stats_payload(
        rows={
            "scufris": {
                "messages": 6,
                "tokens": 250,
                "budget": 4000,
                "history_disabled": False,
                "model": "qwen3",
                "invocations": 3,
                "last_activity": None,
            },
        }
    )
    text = bot.format_telegram_stats(payload)
    assert "*Per-agent:*" in text
    assert "scufris" in text
    assert "6 msgs" in text


def test_format_telegram_stats_handles_iso_string_last_activity() -> None:
    """``last_activity`` arrives as an ISO string over JSON; the renderer
    must coerce it back to ``datetime`` before computing relative time
    (regression for ``TypeError: unsupported operand type(s) for -``).
    """
    payload = _stats_payload(
        rows={
            "scufris": {
                "messages": 2,
                "tokens": 50,
                "budget": None,
                "history_disabled": False,
                "model": "qwen3",
                "invocations": 1,
                "last_activity": "2026-06-03T13:21:11.697547Z",
            },
        }
    )
    # Should not raise.
    text = bot.format_telegram_stats(payload)
    assert "scufris" in text
    # Some "ago" suffix or "just now" — definitely not the raw "—" placeholder.
    assert ("ago" in text) or ("just now" in text)


def test_format_telegram_stats_renders_tool_histogram() -> None:
    payload = _stats_payload(tools={"web_search": 8, "weather": 1})
    text = bot.format_telegram_stats(payload)
    assert "*Tool usage:*" in text
    assert "web_search" in text
    assert "█" in text
    # Bars are inside a code fence.
    assert "```" in text


def test_format_telegram_stats_omits_empty_sections() -> None:
    text = bot.format_telegram_stats(_stats_payload())
    assert "*Per-agent:*" not in text
    assert "*Tool usage:*" not in text


# ----------------------------------------------------------------------
# Thinking-toggle helpers
# ----------------------------------------------------------------------


def test_store_thinking_evicts_oldest_beyond_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bot, "_THINKING_CACHE_MAX", 3)
    bot._thinking_cache.clear()
    for i in range(5):
        bot._store_thinking(i, f"answer-{i}", f"trace-{i}")
    # Only the 3 most-recently-stored entries survive (FIFO eviction).
    assert set(bot._thinking_cache.keys()) == {2, 3, 4}


def test_thinking_keyboard_button_label_reflects_state() -> None:
    collapsed = bot._thinking_keyboard(expanded=False)
    expanded = bot._thinking_keyboard(expanded=True)
    assert "Show thinking" in collapsed.inline_keyboard[0][0].text
    assert collapsed.inline_keyboard[0][0].callback_data == "think:show"
    assert "Hide thinking" in expanded.inline_keyboard[0][0].text
    assert expanded.inline_keyboard[0][0].callback_data == "think:hide"


def test_thinking_toggle_show_expands_message() -> None:
    bot._thinking_cache.clear()
    bot._store_thinking(101, "the answer", "step 1\nstep 2")

    update = MagicMock()
    query = MagicMock()
    query.data = "think:show"
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.message_id = 101
    query.message.text = "the answer"
    query.edit_message_text = AsyncMock()
    update.callback_query = query

    asyncio.run(bot.thinking_toggle(update, MagicMock()))

    query.answer.assert_awaited_once()
    [call] = query.edit_message_text.await_args_list
    new_text = call.args[0]
    assert "the answer" in new_text
    assert "step 1" in new_text
    assert "step 2" in new_text
    assert call.kwargs.get("parse_mode") == "Markdown"


def test_thinking_toggle_hide_collapses_to_answer_only() -> None:
    bot._thinking_cache.clear()
    bot._store_thinking(202, "the answer", "noise")

    update = MagicMock()
    query = MagicMock()
    query.data = "think:hide"
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.message_id = 202
    query.message.text = "the answer\n\n— thinking —\nnoise"
    query.edit_message_text = AsyncMock()
    update.callback_query = query

    asyncio.run(bot.thinking_toggle(update, MagicMock()))

    [call] = query.edit_message_text.await_args_list
    assert call.args[0] == "the answer"


def test_thinking_toggle_stale_cache_renders_expired_notice() -> None:
    bot._thinking_cache.clear()  # nothing cached for this message id

    update = MagicMock()
    query = MagicMock()
    query.data = "think:show"
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.message_id = 999
    query.message.text = "old answer"
    query.edit_message_text = AsyncMock()
    update.callback_query = query

    asyncio.run(bot.thinking_toggle(update, MagicMock()))

    [call] = query.edit_message_text.await_args_list
    assert "expired" in call.args[0].lower()

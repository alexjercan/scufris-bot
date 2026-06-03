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
from typing import Any, AsyncIterator, List
from unittest.mock import AsyncMock, MagicMock

import pytest

# bot.py imports load_config() at import time which validates Telegram
# env vars. Set placeholders before the module is imported.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:fake")
os.environ.setdefault("ALLOWED_USER_IDS", "42")

import bot  # noqa: E402
from scufris_client import StreamEvent  # noqa: E402
from utils.callbacks import ThinkingEvent  # noqa: E402

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

    async def chat_stream(
        self, user_id: int, message: str
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append((user_id, message))
        for ev in self._events:
            yield ev


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
    client = _CmdClient(stats={"lines": ["Scufris session stats", "Uptime: 1m"]})
    monkeypatch.setattr(bot, "_client", client)
    update = _make_update_double(user_id=42)

    asyncio.run(bot.stats_command.__wrapped__(update, MagicMock()))

    assert client.stats_for == [42]
    sent = [c.args[0] for c in update.message.reply_text.await_args_list]
    # Stats are wrapped in a Markdown code fence.
    assert any("Scufris session stats" in t for t in sent)
    assert any(t.startswith("```") for t in sent)

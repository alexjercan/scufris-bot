import asyncio
import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from scufris_cli.__main__ import (
    _handle_command,
    _handle_message,
    _read_input,
    _Settings,
    make_render_thinking,
)
from scufris_client import (
    ScufrisAuthError,
    ScufrisClient,
    ScufrisConnectionError,
    ScufrisServerError,
    StreamEvent,
    ThinkingEvent,
)


@pytest.fixture
def console():
    file = io.StringIO()
    # Use color_system=None to avoid ANSI escape codes in tests
    console = Console(file=file, color_system=None, force_terminal=False, width=80)
    return console, file


@pytest.fixture
def mock_client():
    client = MagicMock(spec=ScufrisClient)
    client.sessions = AsyncMock()
    client.clear = AsyncMock()
    client.chat_stream = MagicMock()
    return client


@pytest.mark.asyncio
async def test_render_thinking_branches(console):
    console, file = console
    settings = _Settings(full_thinking=True)
    render = make_render_thinking(console, settings)

    # 1. tool_call
    ev_call = ThinkingEvent(
        kind="tool_call",
        source="scufris",
        text="bash_tool",
        depth=0,
        arg="ls -la",
    )
    render(ev_call)
    assert "→ Scufris uses Bash Tool: ls -la" in file.getvalue()
    file.truncate(0)
    file.seek(0)

    # 2. tool_result (success)
    ev_res_ok = ThinkingEvent(
        kind="tool_result",
        source="scufris",
        text="bash_tool\nsuccess",
        depth=0,
    )
    render(ev_res_ok)
    assert "↩ Bash Tool" in file.getvalue()
    file.truncate(0)
    file.seek(0)

    # 3. tool_result (failure)
    ev_res_fail = ThinkingEvent(
        kind="tool_result",
        source="scufris",
        text="bash_tool failed: error",
        depth=0,
    )
    render(ev_res_fail)
    assert "✗ bash_tool failed: error" in file.getvalue()
    file.truncate(0)
    file.seek(0)

    # 4. tool_meta
    ev_meta = ThinkingEvent(
        kind="tool_meta",
        source="scufris",
        text="permission granted",
        depth=0,
    )
    render(ev_meta)
    assert "ℹ permission granted" in file.getvalue()
    file.truncate(0)
    file.seek(0)

    # 5. text (delta)
    ev_text = ThinkingEvent(
        kind="text",
        source="scufris",
        text="hello",
        depth=0,
    )
    render(ev_text)
    assert "hello" in file.getvalue()


@pytest.mark.asyncio
async def test_render_thinking_short_mode(console):
    console, file = console
    settings = _Settings(full_thinking=False)
    render = make_render_thinking(console, settings)

    ev_text = ThinkingEvent(
        kind="text",
        source="scufris",
        text="a" * 500,
        depth=0,
    )
    render(ev_text)
    output = file.getvalue().strip()
    # Should be truncated to THINKING_SHORT_LIMIT (240)
    # We check the length of the text itself.
    assert len(output) <= 245  # Allowing some buffer for the ellipsis


@pytest.mark.asyncio
async def test_handle_command_exit(console, mock_client):
    console, _ = console
    should_exit, _ = await _handle_command(
        console, mock_client, 1, "cli", "/exit", False, _Settings()
    )
    assert should_exit is True


@pytest.mark.asyncio
async def test_handle_command_help(console, mock_client):
    console, file = console
    should_exit, _ = await _handle_command(
        console, mock_client, 1, "cli", "/help", False, _Settings()
    )
    assert should_exit is False
    assert "/help" in file.getvalue()


@pytest.mark.asyncio
async def test_handle_command_sessions_success(console, mock_client):
    console, file = console
    mock_client.sessions.return_value = [
        {
            "channel_id": 1,
            "channel": {"surface": "cli", "surface_id": "test", "agent": "build"},
            "last_used_at": 1000,
        },
        {
            "channel_id": 2,
            "channel": {"surface": "cli", "surface_id": "test", "agent": "build"},
            "last_used_at": 2000,
        },
    ]
    should_exit, _ = await _handle_command(
        console, mock_client, 1, "cli", "/sessions", False, _Settings()
    )
    assert should_exit is False
    output = file.getvalue()
    assert "2" in output  # ID of second channel
    assert "1" in output  # ID of first channel


@pytest.mark.asyncio
async def test_handle_command_sessions_empty(console, mock_client):
    console, file = console
    mock_client.sessions.return_value = []
    await _handle_command(
        console, mock_client, 1, "cli", "/sessions", False, _Settings()
    )
    assert "no sessions yet" in file.getvalue()


@pytest.mark.asyncio
async def test_handle_command_clear_success(console, mock_client):
    console, file = console
    mock_client.clear.return_value = {"count": 5}
    should_exit, _ = await _handle_command(
        console, mock_client, 1, "cli", "/clear", False, _Settings()
    )
    assert should_exit is False
    assert "cleared 5 session(s)" in file.getvalue()


@pytest.mark.asyncio
async def test_handle_command_multiline_toggle(console, mock_client):
    console, _ = console
    # Toggle ON
    should_exit, new_multiline = await _handle_command(
        console, mock_client, 1, "cli", "/multiline", False, _Settings()
    )
    assert should_exit is False
    assert new_multiline is True

    # Toggle OFF
    should_exit, new_multiline = await _handle_command(
        console, mock_client, 1, "cli", "/multiline", True, _Settings()
    )
    assert should_exit is False
    assert new_multiline is False


def test_read_input_eof():
    # Mock input to raise EOFError
    from unittest.mock import patch

    with patch("builtins.input", side_effect=EOFError):
        console = Console(file=io.StringIO(), color_system=None)
        res = _read_input(console, False)
        assert res is None


def test_read_input_multiline_finish():
    from unittest.mock import patch

    # First line, then ".", then EOF
    with patch("builtins.input", side_effect=["hello", ".", EOFError]):
        console = Console(file=io.StringIO(), color_system=None)
        res = _read_input(console, True)
        assert res == "hello"


@pytest.mark.asyncio
async def test_handle_command_unknown(console, mock_client):
    console, file = console
    should_exit, _ = await _handle_command(
        console, mock_client, 1, "cli", "/unknown", False, _Settings()
    )
    assert should_exit is False


@pytest.mark.asyncio
async def test_handle_command_thinking_modes(console, mock_client):
    console, file = console
    settings = _Settings()

    # 1. /thinking full
    should_exit, new_multiline = await _handle_command(
        console, mock_client, 1, "cli", "/thinking full", False, settings
    )
    assert should_exit is False
    assert settings.full_thinking is True
    assert "thinking mode: full" in file.getvalue()
    file.truncate(0)
    file.seek(0)

    # 2. /thinking short
    should_exit, new_multiline = await _handle_command(
        console, mock_client, 1, "cli", "/thinking short", False, settings
    )
    assert should_exit is False
    assert settings.full_thinking is False
    assert "thinking mode: short" in file.getvalue()
    file.truncate(0)
    file.seek(0)

    # 3. /thinking invalid
    should_exit, new_multiline = await _handle_command(
        console, mock_client, 1, "cli", "/thinking invalid", False, settings
    )
    assert should_exit is False
    assert "unknown thinking mode" in file.getvalue()


@pytest.mark.asyncio
async def test_handle_command_sessions_degraded(console, mock_client):
    console, file = console
    # user_id is None
    should_exit, _ = await _handle_command(
        console, mock_client, None, "cli", "/sessions", False, _Settings()
    )
    assert should_exit is False
    assert "identity unavailable" in file.getvalue()


@pytest.mark.asyncio
async def test_handle_command_clear_degraded(console, mock_client):
    console, file = console
    # user_id is None
    should_exit, _ = await _handle_command(
        console, mock_client, None, "cli", "/clear", False, _Settings()
    )
    assert should_exit is False
    assert "identity unavailable" in file.getvalue()


@pytest.mark.asyncio
async def test_handle_message_success(console, mock_client):
    console, file = console
    logger = MagicMock()

    # Mocking async generator for chat_stream
    async def mock_stream(*args, **kwargs):
        yield StreamEvent(
            kind="thinking",
            thinking=ThinkingEvent(
                kind="text", text="thinking", source="scufris", depth=0
            ),
        )
        yield StreamEvent(kind="done", text="hello world")

    mock_client.chat_stream.side_effect = mock_stream

    await _handle_message(
        console, mock_client, "test-surface", "hello", lambda x: None, logger
    )

    output = file.getvalue()
    assert "hello world" in output
    # Check if it printed a panel (roughly)
    assert "scufris" in output


@pytest.mark.asyncio
async def test_handle_message_error_event(console, mock_client):
    console, file = console
    logger = MagicMock()

    async def mock_stream(*args, **kwargs):
        yield StreamEvent(
            kind="error", error="something went wrong", error_type="api_error"
        )

    mock_client.chat_stream.side_effect = mock_stream

    await _handle_message(
        console, mock_client, "test-surface", "hello", lambda x: None, logger
    )

    assert "something went wrong" in file.getvalue()
    assert "api_error" in file.getvalue()


@pytest.mark.asyncio
async def test_handle_message_connection_error(console, mock_client):
    console, file = console
    logger = MagicMock()

    mock_client.chat_stream.side_effect = ScufrisConnectionError("connection refused")

    await _handle_message(
        console, mock_client, "test-surface", "hello", lambda x: None, logger
    )


@pytest.mark.asyncio
async def test_handle_message_auth_error(console, mock_client):
    console, file = console
    logger = MagicMock()

    mock_client.chat_stream.side_effect = ScufrisAuthError("auth failed")

    await _handle_message(
        console, mock_client, "test-surface", "hello", lambda x: None, logger
    )

    assert "auth failed" in file.getvalue()


@pytest.mark.asyncio
async def test_handle_message_server_error(console, mock_client):
    console, file = console
    logger = MagicMock()

    mock_client.chat_stream.side_effect = ScufrisServerError("server error")

    await _handle_message(
        console, mock_client, "test-surface", "hello", lambda x: None, logger
    )

    assert "server error" in file.getvalue()


@pytest.mark.asyncio
async def test_handle_message_cancelled(console, mock_client):
    console, file = console
    logger = MagicMock()

    mock_client.chat_stream.side_effect = asyncio.CancelledError()

    await _handle_message(
        console, mock_client, "test-surface", "hello", lambda x: None, logger
    )

    assert "interrupted — server canceled" in file.getvalue()

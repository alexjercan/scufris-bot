import asyncio
import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console
from scufris_client import (
    ScufrisClient,
    ScufrisError,
    StreamEvent,
    ThinkingEvent,
)
from scufris_cli.__main__ import (
    _handle_command,
    make_render_thinking,
    _Settings,
    _read_input,
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
    client.chat_stream = AsyncMock()
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
    assert "unknown command" in file.getvalue()

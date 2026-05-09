"""Unit tests for utils.stats formatters and table rendering."""

from datetime import datetime, timedelta, timezone

import pytest

from utils.stats import format_relative, format_stats_lines, format_uptime

# ---------------------------------------------------------------------------
# format_relative
# ---------------------------------------------------------------------------


NOW = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_format_relative_none_returns_dash():
    assert format_relative(None) == "—"


def test_format_relative_future_timestamp_returns_just_now():
    assert format_relative(NOW + timedelta(seconds=5), now=NOW) == "just now"


@pytest.mark.parametrize(
    "delta_secs,expected",
    [
        (0, "0s ago"),
        (45, "45s ago"),
        (60, "1m ago"),
        (59 * 60, "59m ago"),
        (60 * 60, "1h ago"),
        (23 * 3600, "23h ago"),
        (24 * 3600, "1d ago"),
        (5 * 86400, "5d ago"),
    ],
)
def test_format_relative_thresholds(delta_secs, expected):
    ts = NOW - timedelta(seconds=delta_secs)
    assert format_relative(ts, now=NOW) == expected


# ---------------------------------------------------------------------------
# format_uptime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secs,expected",
    [
        (0, "0s"),
        (45, "45s"),
        (60, "1m 0s"),
        (125, "2m 5s"),
        (3600, "1h 0m"),
        (3 * 3600 + 25 * 60, "3h 25m"),
        (86400, "1d 0h"),
        (2 * 86400 + 5 * 3600, "2d 5h"),
    ],
)
def test_format_uptime_thresholds(secs, expected):
    started = NOW - timedelta(seconds=secs)
    assert format_uptime(started, now=NOW) == expected


def test_format_uptime_clamps_negative_to_zero():
    # Started "in the future" — clamps to 0s.
    assert format_uptime(NOW + timedelta(seconds=5), now=NOW) == "0s"


# ---------------------------------------------------------------------------
# format_stats_lines
# ---------------------------------------------------------------------------


class _FakeHistoryManager:
    def __init__(self, stats, telemetry):
        self._stats = stats
        self._telemetry = telemetry

    def get_stats(self):
        return self._stats

    def get_user_telemetry(self, _user_id):
        return self._telemetry


def _render(telemetry, stats=None):
    stats = stats or {
        "total_invocations": sum(t["invocations"] for t in telemetry.values()),
        "total_messages": sum(t["messages"] for t in telemetry.values()),
        "messages_per_agent": {a: t["messages"] for a, t in telemetry.items()},
    }
    hm = _FakeHistoryManager(stats, telemetry)
    started_at = NOW - timedelta(minutes=10)
    return format_stats_lines(
        hm, user_id=1, started_at=started_at, model="qwen3", base_url="http://stub"
    )


def test_empty_manager_renders_no_agents_line():
    lines = _render({})
    assert "Per-agent:" in lines
    assert "  (no agents registered)" in lines
    assert any("Totals: 0 messages across 0 agent(s)" in line for line in lines)


def test_history_disabled_agent_renders_marker():
    tel = {
        "utilities_agent": {
            "messages": 0,
            "tokens": 0,
            "budget": None,
            "history_disabled": True,
            "model": "qwen3",
            "invocations": 2,
            "last_activity": None,
        },
    }
    lines = _render(tel)
    assert any("(history disabled)" in line for line in lines)


def test_zero_message_history_enabled_agent_renders_zero_msgs():
    tel = {
        "knowledge_agent": {
            "messages": 0,
            "tokens": 0,
            "budget": 4000,
            "history_disabled": False,
            "model": "qwen3",
            "invocations": 0,
            "last_activity": None,
        },
    }
    lines = _render(tel)
    assert any("0 msgs" in line and "(history disabled)" not in line for line in lines)


def test_memory_cell_with_budget_shows_pct_of_budget():
    tel = {
        "knowledge_agent": {
            "messages": 4,
            "tokens": 800,
            "budget": 4000,
            "history_disabled": False,
            "model": "qwen3",
            "invocations": 1,
            "last_activity": NOW,
        },
    }
    lines = _render(tel)
    cell_line = next(line for line in lines if "knowledge_agent" in line)
    assert "% of 4000" in cell_line
    assert "4 msgs" in cell_line
    assert "~800 tok" in cell_line


def test_memory_cell_without_budget_shows_only_token_count():
    tel = {
        "scufris": {
            "messages": 6,
            "tokens": 250,
            "budget": None,
            "history_disabled": False,
            "model": "qwen3",
            "invocations": 3,
            "last_activity": NOW,
        },
    }
    lines = _render(tel)
    cell_line = next(line for line in lines if "scufris" in line and "qwen3" in line)
    assert "~250 tok" in cell_line
    assert "%" not in cell_line


def test_history_enabled_agents_listed_before_disabled_alphabetical():
    tel = {
        "utilities_agent": {
            "messages": 0,
            "tokens": 0,
            "budget": None,
            "history_disabled": True,
            "model": "qwen3",
            "invocations": 1,
            "last_activity": NOW,
        },
        "knowledge_agent": {
            "messages": 1,
            "tokens": 5,
            "budget": 4000,
            "history_disabled": False,
            "model": "qwen3",
            "invocations": 1,
            "last_activity": NOW,
        },
        "coding_agent": {
            "messages": 1,
            "tokens": 5,
            "budget": 4000,
            "history_disabled": False,
            "model": "qwen3",
            "invocations": 1,
            "last_activity": NOW,
        },
    }
    lines = _render(tel)
    # Find line indices of each agent
    body = [
        line
        for line in lines
        if any(
            a in line for a in ("coding_agent", "knowledge_agent", "utilities_agent")
        )
    ]
    # Order: coding, knowledge (history-enabled alphabetical), then utilities.
    names = [
        next(
            a
            for a in ("coding_agent", "knowledge_agent", "utilities_agent")
            if a in line
        )
        for line in body
    ]
    assert names == ["coding_agent", "knowledge_agent", "utilities_agent"]


def test_separator_width_matches_widest_column():
    # Make a model name wider than the header "model" (len 5).
    long_model = "a-very-long-model-name-7b"

    def render_with_model(m):
        tel = {
            "knowledge_agent": {
                "messages": 1,
                "tokens": 5,
                "budget": 4000,
                "history_disabled": False,
                "model": m,
                "invocations": 1,
                "last_activity": NOW,
            },
        }
        return _render(tel)

    short_lines = render_with_model("m")
    long_lines = render_with_model(long_model)

    def sep_after_header(lines):
        idx = next(
            i for i, line in enumerate(lines) if line.lstrip().startswith("agent")
        )
        return lines[idx + 1]

    short_sep = sep_after_header(short_lines)
    long_sep = sep_after_header(long_lines)
    # Long model must shift the separator wider by the extra chars.
    assert len(long_sep) > len(short_sep)
    assert "─" in long_sep
    # Each column's underline is contiguous "─" runs joined by gutters.
    # The widest run in the long version must be >= len(long_model).
    runs = [r for r in long_sep.split() if set(r) == {"─"}]
    assert max(len(r) for r in runs) >= len(long_model)


def test_calls_column_right_aligned():
    # Two agents; one with a multi-digit invocation count.
    tel = {
        "knowledge_agent": {
            "messages": 1,
            "tokens": 5,
            "budget": 4000,
            "history_disabled": False,
            "model": "m",
            "invocations": 999,
            "last_activity": NOW,
        },
        "coding_agent": {
            "messages": 1,
            "tokens": 5,
            "budget": 4000,
            "history_disabled": False,
            "model": "m",
            "invocations": 1,
            "last_activity": NOW,
        },
    }
    lines = _render(tel)
    coding = next(line for line in lines if "coding_agent" in line)
    # The "1" should be right-aligned in a 5-wide column ("calls" header).
    # i.e. preceded by spaces relative to where a "999" would sit.
    assert "    1  " in coding or "  1  " in coding

"""Stats rendering helpers for /stats command (CLI + Telegram).

Builds a single source of truth for the human-readable session
dashboard so the CLI and Telegram handlers stay in sync.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional


def format_relative(ts: Optional[datetime], now: Optional[datetime] = None) -> str:
    """Format a UTC timestamp as 'Xs/m/h/d ago'. Returns '—' when None."""
    if ts is None:
        return "—"
    now = now or datetime.utcnow()
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def format_uptime(started_at: datetime, now: Optional[datetime] = None) -> str:
    """Format an uptime delta as e.g. '1h 23m' or '45s'."""
    now = now or datetime.utcnow()
    secs = max(0, int((now - started_at).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    mins, s = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {s}s"
    hours, m = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {m}m"
    days, h = divmod(hours, 24)
    return f"{days}d {h}h"


def format_stats_lines(
    history_manager,
    user_id: int,
    started_at: datetime,
    model: str,
    base_url: str,
) -> List[str]:
    """Return the /stats output as a list of lines (no trailing newline).

    Format is monospace-friendly so it renders identically in the CLI and
    in a Telegram ``` code block.

    Per-agent rows are rendered as an aligned table with a header row.
    Column widths are computed from the data each render so additions
    or longer names/models don't break alignment.
    """
    stats = history_manager.get_stats()
    telemetry = history_manager.get_user_telemetry(user_id)

    lines: List[str] = [
        "Scufris session stats",
        "─" * 21,
        f"Uptime:                {format_uptime(started_at)}",
        f"Model (default):       {model} @ {base_url}",
        f"Total invocations:     {stats['total_invocations']}",
        "",
        "Per-agent:",
    ]

    if not telemetry:
        lines.append("  (no agents registered)")
    else:
        # Sort: history-enabled first (alphabetical), then disabled.
        ordered = sorted(
            telemetry.items(),
            key=lambda kv: (kv[1]["history_disabled"], kv[0]),
        )

        # Build all rows up front so we can compute column widths from
        # the actual rendered cell contents (memory cell varies a lot).
        header = ("agent", "model", "memory", "calls", "last")
        rows: List[tuple] = [header]
        for agent, t in ordered:
            model_cell = t.get("model") or "—"
            calls_cell = str(t["invocations"])
            last_cell = format_relative(t["last_activity"])

            if t["history_disabled"]:
                memory_cell = "(history disabled)"
            elif t["messages"] == 0:
                memory_cell = "0 msgs"
            else:
                msgs = t["messages"]
                tokens = t["tokens"]
                budget = t["budget"]
                if budget:
                    pct = (tokens * 100) // budget if budget else 0
                    memory_cell = f"{msgs} msgs / ~{tokens} tok ({pct}% of {budget})"
                else:
                    memory_cell = f"{msgs} msgs / ~{tokens} tok"

            rows.append((agent, model_cell, memory_cell, calls_cell, last_cell))

        # Compute width per column from the data (header included).
        widths = [max(len(row[i]) for row in rows) for i in range(len(header))]

        # Render: header, separator, then data rows. Two-space gutters.
        gutter = "  "

        def fmt_row(row: tuple) -> str:
            agent_c, model_c, mem_c, calls_c, last_c = row
            return (
                f"  {agent_c:<{widths[0]}}{gutter}"
                f"{model_c:<{widths[1]}}{gutter}"
                f"{mem_c:<{widths[2]}}{gutter}"
                f"{calls_c:>{widths[3]}}{gutter}"
                f"{last_c:<{widths[4]}}"
            ).rstrip()

        lines.append(fmt_row(header))
        # Underline row matching header column widths.
        lines.append("  " + gutter.join("─" * w for w in widths))
        for row in rows[1:]:
            lines.append(fmt_row(row))

    lines.append("")
    lines.append(
        f"Totals: {stats['total_messages']} messages across "
        f"{len(stats['messages_per_agent'])} agent(s)"
    )
    return lines

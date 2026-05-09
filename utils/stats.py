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

        # Column widths — recomputed per render so long agent names
        # don't break alignment if we ever add more.
        name_w = max(len(a) for a, _ in ordered)
        model_w = max(len((t.get("model") or "—")) for _, t in ordered)

        for agent, t in ordered:
            agent_col = f"{agent:<{name_w}}"
            model_col = f"{(t.get('model') or '—'):<{model_w}}"
            calls = t["invocations"]
            last = format_relative(t["last_activity"])
            tail = f"calls={calls}   last={last}"

            if t["history_disabled"]:
                memory_col = "(history disabled)"
            elif t["messages"] == 0:
                memory_col = "0 msgs"
            else:
                msgs = t["messages"]
                tokens = t["tokens"]
                budget = t["budget"]
                if budget:
                    pct = (tokens * 100) // budget if budget else 0
                    memory_col = f"{msgs} msgs / ~{tokens} tok ({pct}% of {budget})"
                else:
                    memory_col = f"{msgs} msgs / ~{tokens} tok"

            lines.append(f"  {agent_col}  [{model_col}]  {memory_col:<38}  {tail}")

    lines.append("")
    lines.append(
        f"Totals: {stats['total_messages']} messages across "
        f"{len(stats['messages_per_agent'])} agent(s)"
    )
    return lines

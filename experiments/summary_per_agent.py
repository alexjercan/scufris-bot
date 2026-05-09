"""Per-sub-agent summary stats from the telemetry JSONL log.

Reports for each ``child_agent``:

- call count
- refusal rate (``outcome == "refused"``)
- error rate (``outcome == "error"``)
- mean / p50 / p95 of ``query_chars``, ``context_chars``,
  ``duration_ms``

Telemetry collection is off by default. Enable with::

    SCUFRIS_TELEMETRY=1 python cli.py
    SCUFRIS_TELEMETRY=1 python main.py

pandas is declared in ``pyproject.toml`` but may not be installed
in the active env. Install with ``uv sync`` or
``pip install pandas`` if you hit an ``ImportError``.

Usage::

    python experiments/summary_per_agent.py
    python experiments/summary_per_agent.py path/to/log.jsonl
"""

from __future__ import annotations

import sys

from _common import load_events, parse_path_arg


def main() -> int:
    df = load_events(parse_path_arg())
    if df.empty:
        print("no events")
        return 0

    grouped = df.groupby("child_agent")
    rows = []
    for name, g in grouped:
        n = len(g)
        refused = int((g["outcome"] == "refused").sum())
        errored = int((g["outcome"] == "error").sum())
        rows.append(
            {
                "agent": name,
                "calls": str(n),
                "refused%": f"{(refused / n) * 100:.0f}",
                "error%": f"{(errored / n) * 100:.0f}",
                "q_mean": f"{g['query_chars'].mean():.0f}",
                "q_p50": f"{g['query_chars'].quantile(0.5):.0f}",
                "q_p95": f"{g['query_chars'].quantile(0.95):.0f}",
                "ctx_mean": f"{g['context_chars'].mean():.0f}",
                "ctx_p50": f"{g['context_chars'].quantile(0.5):.0f}",
                "ctx_p95": f"{g['context_chars'].quantile(0.95):.0f}",
                "dur_mean_ms": f"{g['duration_ms'].mean():.0f}",
                "dur_p50_ms": f"{g['duration_ms'].quantile(0.5):.0f}",
                "dur_p95_ms": f"{g['duration_ms'].quantile(0.95):.0f}",
            }
        )

    cols = list(rows[0].keys())
    widths = {c: max(len(c), max(len(r[c]) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    sep = "  ".join("─" * widths[c] for c in cols)
    print(f"Per-agent summary  ({len(df)} events total)\n")
    print(header)
    print(sep)
    for r in rows:
        print("  ".join(r[c].ljust(widths[c]) for c in cols))
    return 0


if __name__ == "__main__":
    sys.exit(main())

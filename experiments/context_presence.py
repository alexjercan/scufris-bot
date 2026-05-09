"""Context-presence analysis from the telemetry JSONL log.

Question: does giving a sub-agent ``context`` reduce its refusal
rate? For each ``child_agent`` we report:

- count of calls with vs without context
- refusal rate when ``context_present`` is true vs false

Telemetry collection is off by default. Enable with::

    SCUFRIS_TELEMETRY=1 python cli.py
    SCUFRIS_TELEMETRY=1 python main.py

pandas is declared in ``pyproject.toml`` but may not be installed
in the active env. Install with ``uv sync`` or
``pip install pandas`` if you hit an ``ImportError``.

Usage::

    python experiments/context_presence.py
    python experiments/context_presence.py path/to/log.jsonl
"""

from __future__ import annotations

import sys

from _common import load_events, parse_path_arg


def _refusal_rate(g) -> str:
    if len(g) == 0:
        return "—"
    return f"{((g['outcome'] == 'refused').sum() / len(g)) * 100:.0f}%"


def main() -> int:
    df = load_events(parse_path_arg())
    if df.empty:
        print("no events")
        return 0

    rows = []
    for name, g in df.groupby("child_agent"):
        with_ctx = g[g["context_present"]]
        without_ctx = g[~g["context_present"]]
        n = len(g)
        rows.append(
            {
                "agent": name,
                "calls": str(n),
                "with_ctx": str(len(with_ctx)),
                "without_ctx": str(len(without_ctx)),
                "ctx%": f"{(len(with_ctx) / n) * 100:.0f}",
                "refused|ctx": _refusal_rate(with_ctx),
                "refused|no_ctx": _refusal_rate(without_ctx),
            }
        )

    cols = list(rows[0].keys())
    widths = {c: max(len(c), max(len(r[c]) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    sep = "  ".join("─" * widths[c] for c in cols)
    print(f"Context presence vs refusal  ({len(df)} events total)\n")
    print(header)
    print(sep)
    for r in rows:
        print("  ".join(r[c].ljust(widths[c]) for c in cols))
    return 0


if __name__ == "__main__":
    sys.exit(main())

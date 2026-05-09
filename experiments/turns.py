"""Turn-level analysis from the telemetry JSONL log.

Groups events by ``turn_id`` and reports:

- distribution of delegations per turn (1, 2, 3+)
- top-N agent combinations co-occurring in a single turn
  (an unordered frozenset of ``child_agent`` names)
- mean / p95 of total ``query_chars + context_chars`` per turn

Telemetry collection is off by default. Enable with::

    SCUFRIS_TELEMETRY=1 python cli.py
    SCUFRIS_TELEMETRY=1 python main.py

pandas is declared in ``pyproject.toml`` but may not be installed
in the active env. Install with ``uv sync`` or
``pip install pandas`` if you hit an ``ImportError``.

Usage::

    python experiments/turns.py
    python experiments/turns.py path/to/log.jsonl
"""

from __future__ import annotations

import sys
from collections import Counter

from _common import load_events, parse_path_arg

TOP_COMBOS = 10


def main() -> int:
    df = load_events(parse_path_arg())
    if df.empty:
        print("no events")
        return 0

    # Drop events with no turn_id (shouldn't happen post-spike, but
    # be defensive — older logs from before begin_turn() wiring may
    # have null turn_ids).
    df = df.dropna(subset=["turn_id"])
    if df.empty:
        print("no events with turn_id")
        return 0

    per_turn = df.groupby("turn_id")
    sizes = per_turn.size()

    print(f"Turns: {len(sizes)} | events: {len(df)}\n")

    # Delegations-per-turn distribution
    print("Delegations per turn:")
    bins: Counter = Counter()
    for n in sizes:
        bins["1" if n == 1 else "2" if n == 2 else "3+"] += 1
    total = sum(bins.values())
    for label in ("1", "2", "3+"):
        c = bins.get(label, 0)
        pct = (c / total) * 100 if total else 0
        print(f"  {label:>3}  {c:>4}  ({pct:.0f}%)")
    print()

    # Top agent combos
    print(f"Top {TOP_COMBOS} agent combos per turn:")
    combos: Counter = Counter()
    for _, g in per_turn:
        combo = frozenset(g["child_agent"].tolist())
        combos[combo] += 1
    for combo, count in combos.most_common(TOP_COMBOS):
        label = " + ".join(sorted(combo)) if combo else "(empty)"
        print(f"  {count:>4}  {label}")
    print()

    # Total chars per turn
    df = df.copy()
    df["total_chars"] = df["query_chars"] + df["context_chars"]
    chars_per_turn = df.groupby("turn_id")["total_chars"].sum()
    print("Total chars per turn (query + context):")
    print(f"  mean  {chars_per_turn.mean():.0f}")
    print(f"  p50   {chars_per_turn.quantile(0.5):.0f}")
    print(f"  p95   {chars_per_turn.quantile(0.95):.0f}")
    print(f"  max   {chars_per_turn.max():.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

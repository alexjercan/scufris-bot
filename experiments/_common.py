"""Shared helpers for the telemetry experiment scripts.

Loads `logs/sub_agent_telemetry.jsonl` into a pandas DataFrame.
Handles the two unhappy paths we care about (missing file,
empty file) so the individual scripts can stay focused on
their analysis.

Telemetry collection is off by default. Enable with::

    SCUFRIS_TELEMETRY=1 python cli.py
    SCUFRIS_TELEMETRY=1 python main.py

pandas is declared in ``pyproject.toml`` but may not be installed
in the active env. Install with ``uv sync`` or
``pip install pandas`` if you hit an ``ImportError`` below.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

DEFAULT_LOG = (
    Path(__file__).resolve().parent.parent / "logs" / "sub_agent_telemetry.jsonl"
)


def load_events(path: Optional[Path] = None):
    """Read the JSONL log into a pandas DataFrame.

    On missing file: prints an error and `sys.exit(1)`.
    On empty file: returns an empty DataFrame (caller should print
    "no events" and exit 0).
    """
    log_path = Path(path) if path is not None else DEFAULT_LOG
    if not log_path.exists():
        print(
            f"error: telemetry log not found at {log_path}\n"
            "Enable telemetry with SCUFRIS_TELEMETRY=1 and run the bot/CLI "
            "to generate events.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Import pandas *after* the file check so the missing-file error
    # path works in envs without pandas installed.
    import pandas as pd

    rows = []
    with log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # Skip malformed lines silently — telemetry is best-effort.
                continue
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def parse_path_arg() -> Optional[Path]:
    """Tiny CLI: optional positional path argument, no flags."""
    if len(sys.argv) <= 1:
        return None
    if sys.argv[1] in ("-h", "--help"):
        print(f"usage: {sys.argv[0]} [path-to-jsonl]")
        sys.exit(0)
    return Path(sys.argv[1])

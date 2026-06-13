"""Apply scufris-server migrations to a fresh temp DB and report results.

Purpose
-------
Verify the migration runner end-to-end against a throwaway SQLite
file: apply all pending migrations, list the resulting tables, show
the ``_schema_migrations`` tracking rows, then prove idempotency by
re-running and confirming nothing new is applied.

How to run
----------
From the repo root::

    python examples/check_schema.py

Expected output
---------------
- "first run applied: ['001_initial.sql']"
- A list of 6 schema tables plus ``_schema_migrations``
- One tracking row for ``001_initial.sql``
- "second run applied: []"  (idempotent)
- "OK" footer

Requires
--------
Just the ``scufris_server`` package. No opencode, no network.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from scufris_server.config import Settings
from scufris_server.store import apply_migrations, connect


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="scufris-check-schema-") as td:
        state_dir = Path(td)
        settings = Settings(state_dir=state_dir)

        print(f"state_dir: {state_dir}")
        print(f"db_path:   {state_dir / 'scufris.sqlite'}")
        print()

        # First run: should apply 001_initial.sql.
        with connect(settings) as conn:
            applied = apply_migrations(conn)
            print(f"first run applied: {applied}")
            if applied != ["001_initial.sql"]:
                print(f"ERROR: expected ['001_initial.sql'], got {applied}")
                return 1

            print()
            print("tables in DB:")
            rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
            for row in rows:
                print(f"  - {row['name']}")

            print()
            print("_schema_migrations rows:")
            tracking = conn.execute(
                "SELECT filename, applied_at FROM _schema_migrations ORDER BY filename"
            ).fetchall()
            for row in tracking:
                print(f"  - {row['filename']} @ {row['applied_at']}")

        # Second run: should be a no-op.
        with connect(settings) as conn:
            applied2 = apply_migrations(conn)
            print()
            print(f"second run applied: {applied2}")
            if applied2:
                print(f"ERROR: expected [], got {applied2}")
                return 1

    print()
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

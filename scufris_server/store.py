"""SQLite storage and migrations for scufris-server.

Connection model
----------------
We open one ``sqlite3.Connection`` per request via
:func:`scufris_server.dependencies.get_db_conn` (used as a FastAPI
dependency) and close it at request end. Non-request callers — CLI
tools, migrations, tests — use :func:`connect` as a context manager.
Connections are never shared across threads or async tasks.

Why per-request: ``sqlite3`` connections default to
``check_same_thread=True``, and FastAPI dispatches sync handlers across
a thread pool. Opening per request sidesteps the threading question and
lets WAL handle concurrency. With WAL the open cost is microseconds —
negligible relative to the work each request performs.

Concurrency
-----------
WAL is enabled per connection (``PRAGMA journal_mode=WAL``). Writers do
not block readers; multiple scufris-server processes against the same
DB file are safe (single-host, multi-worker). Foreign keys are enforced
per connection (``PRAGMA foreign_keys=ON``).

Transactions
------------
Connections use ``autocommit=False`` (Python 3.12+ explicit transaction
control). Use ``with conn:`` for atomic blocks; commit on success,
rollback on exception. Each migration's DDL and its tracking-row insert
land in a single transaction, so partial application is impossible.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from scufris_server.config import Settings, get_settings

DB_FILENAME = "scufris.sqlite"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_TRACKING_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS _schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at INTEGER NOT NULL
)
"""


def _db_path(settings: Settings) -> Path:
    return settings.state_dir / DB_FILENAME


def _open(path: Path) -> sqlite3.Connection:
    """Open a sqlite connection with our standard PRAGMAs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Connect in autocommit mode so PRAGMAs (notably journal_mode=WAL,
    # which sqlite refuses to change inside a transaction) run cleanly.
    # Then switch to explicit transaction control for the rest of the
    # connection's life.
    conn = sqlite3.connect(path, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    # Wait up to 5s for a contended write lock before raising.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.autocommit = False
    return conn


@contextmanager
def connect(settings: Settings | None = None) -> Iterator[sqlite3.Connection]:
    """Open a connection, yield it, close on exit.

    For CLI tools, migrations, and tests. FastAPI request handlers
    should depend on
    :func:`scufris_server.dependencies.get_db_conn` instead, which
    routes through request-scoped settings.
    """
    s = settings or get_settings()
    conn = _open(_db_path(s))
    try:
        yield conn
    finally:
        conn.close()


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply every pending migration in lexicographic filename order.

    Each ``.sql`` file under :data:`MIGRATIONS_DIR` is run exactly once.
    Successful application is recorded in ``_schema_migrations``; that
    insert lives in the same transaction as the DDL, so a crash can
    never leave the schema applied without the tracking row (or vice
    versa).

    Returns the list of filenames newly applied this call (``[]`` if
    everything was already up to date).
    """
    with conn:
        conn.execute(_TRACKING_TABLE_DDL)

    applied = {
        row[0] for row in conn.execute("SELECT filename FROM _schema_migrations")
    }
    pending = [p for p in sorted(MIGRATIONS_DIR.glob("*.sql")) if p.name not in applied]

    newly_applied: list[str] = []
    for path in pending:
        sql = path.read_text(encoding="utf-8")
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO _schema_migrations (filename, applied_at) "
                "VALUES (?, strftime('%s','now'))",
                (path.name,),
            )
        newly_applied.append(path.name)
    return newly_applied

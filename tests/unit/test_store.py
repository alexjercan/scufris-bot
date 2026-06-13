"""Tests for ``scufris_server.store`` (step 3 of #9)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from scufris_server.config import Settings
from scufris_server.store import (
    DB_FILENAME,
    MIGRATIONS_DIR,
    apply_migrations,
    connect,
)

ALL_TABLES = (
    "users",
    "surface_bindings",
    "channels",
    "session_links",
    "facts",
    "event_log",
)


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    """Settings pointing at an isolated state dir for one test."""
    return Settings(state_dir=tmp_path)


@pytest.fixture
def conn(tmp_settings: Settings) -> Iterator[sqlite3.Connection]:
    """Migrated connection, ready for inserts."""
    with connect(tmp_settings) as c:
        apply_migrations(c)
        yield c


def _table_names(c: sqlite3.Connection) -> set[str]:
    rows = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def test_migrations_dir_ships_initial_sql() -> None:
    """The migration file must exist alongside the package."""
    assert (MIGRATIONS_DIR / "001_initial.sql").is_file()


def test_apply_migrations_creates_every_design_table(
    tmp_settings: Settings,
) -> None:
    with connect(tmp_settings) as c:
        applied = apply_migrations(c)
        names = _table_names(c)

    assert applied == ["001_initial.sql"]
    for table in ALL_TABLES:
        assert table in names, f"{table} missing after migration"
    assert "_schema_migrations" in names


def test_apply_migrations_is_idempotent(tmp_settings: Settings) -> None:
    with connect(tmp_settings) as c:
        first = apply_migrations(c)
        second = apply_migrations(c)
        third = apply_migrations(c)
    assert first == ["001_initial.sql"]
    assert second == []
    assert third == []


def test_apply_migrations_records_tracking_row(tmp_settings: Settings) -> None:
    before = int(time.time())
    with connect(tmp_settings) as c:
        apply_migrations(c)
        rows = c.execute(
            "SELECT filename, applied_at FROM _schema_migrations"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["filename"] == "001_initial.sql"
    # strftime('%s','now') returns unix seconds; loose lower bound only.
    assert rows[0]["applied_at"] >= before


def test_db_file_lives_under_state_dir(tmp_path: Path) -> None:
    s = Settings(state_dir=tmp_path)
    with connect(s) as c:
        apply_migrations(c)
    assert (tmp_path / DB_FILENAME).is_file()


# ---------------------------------------------------------------------------
# PRAGMAs
# ---------------------------------------------------------------------------


def test_journal_mode_is_wal(conn: sqlite3.Connection) -> None:
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert enabled == 1


# ---------------------------------------------------------------------------
# Per-table CRUD smoke
# ---------------------------------------------------------------------------


def test_users_insert_select(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO users (username, created_at) VALUES (?, ?)", ("alex", now)
    )
    conn.commit()
    row = conn.execute(
        "SELECT id, username, created_at FROM users WHERE username=?", ("alex",)
    ).fetchone()
    assert row["username"] == "alex"
    assert row["created_at"] == now
    assert row["id"] == 1


def test_users_username_is_unique(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO users (username, created_at) VALUES (?, ?)", ("alex", now)
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO users (username, created_at) VALUES (?, ?)",
            ("alex", now),
        )


def test_surface_bindings_insert_and_pk(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO users (id, username, created_at) VALUES (1, 'alex', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO surface_bindings (user_id, surface, surface_id) VALUES (?, ?, ?)",
        (1, "telegram", "8231376426"),
    )
    conn.commit()
    row = conn.execute(
        "SELECT user_id FROM surface_bindings WHERE surface=? AND surface_id=?",
        ("telegram", "8231376426"),
    ).fetchone()
    assert row["user_id"] == 1

    # PK is (surface, surface_id) — re-binding the same pair must fail.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO surface_bindings (user_id, surface, surface_id) "
            "VALUES (?, ?, ?)",
            (1, "telegram", "8231376426"),
        )


def test_channels_insert_and_unique(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO users (id, username, created_at) VALUES (1, 'alex', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO channels (user_id, surface, surface_id, agent) "
        "VALUES (?, ?, ?, ?)",
        (1, "telegram", "chat_42", "scufris"),
    )
    # Different agent on the same channel is a different row.
    conn.execute(
        "INSERT INTO channels (user_id, surface, surface_id, agent) "
        "VALUES (?, ?, ?, ?)",
        (1, "telegram", "chat_42", "researcher"),
    )
    conn.commit()
    rows = conn.execute("SELECT id, agent FROM channels ORDER BY id").fetchall()
    assert [r["agent"] for r in rows] == ["scufris", "researcher"]

    # Same (user, surface, surface_id, agent) is rejected.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO channels (user_id, surface, surface_id, agent) "
            "VALUES (?, ?, ?, ?)",
            (1, "telegram", "chat_42", "scufris"),
        )


def test_session_links_insert(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO users (id, username, created_at) VALUES (1, 'alex', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO channels (id, user_id, surface, surface_id, agent) "
        "VALUES (1, 1, 'cli', 'tty1', 'scufris')"
    )
    conn.execute(
        "INSERT INTO session_links "
        "(channel_id, oc_session_id, created_at, last_used_at) "
        "VALUES (?, ?, ?, ?)",
        (1, "ses_abc123", now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT oc_session_id, last_used_at FROM session_links WHERE channel_id=1"
    ).fetchone()
    assert row["oc_session_id"] == "ses_abc123"
    assert row["last_used_at"] == now


def test_facts_insert_and_per_user_key_unique(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO users (id, username, created_at) VALUES (1, 'alex', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO users (id, username, created_at) VALUES (2, 'bob', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO facts "
        "(user_id, key, value, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (1, "favorite_color", "blue", now, now),
    )
    # Same key, different user → fine.
    conn.execute(
        "INSERT INTO facts "
        "(user_id, key, value, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (2, "favorite_color", "green", now, now),
    )
    conn.commit()

    rows = conn.execute(
        "SELECT user_id, value FROM facts WHERE key='favorite_color' ORDER BY user_id"
    ).fetchall()
    assert [(r["user_id"], r["value"]) for r in rows] == [
        (1, "blue"),
        (2, "green"),
    ]

    # Same (user, key) is rejected.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO facts "
            "(user_id, key, value, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, "favorite_color", "red", now, now),
        )


def test_event_log_insert(conn: sqlite3.Connection) -> None:
    ts_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO event_log "
        "(ts, oc_session_id, event_type, payload_json) "
        "VALUES (?, ?, ?, ?)",
        (ts_ms, "ses_abc", "message.part.delta", '{"text":"hello"}'),
    )
    # oc_session_id may be NULL (per schema).
    conn.execute(
        "INSERT INTO event_log "
        "(ts, oc_session_id, event_type, payload_json) "
        "VALUES (?, ?, ?, ?)",
        (ts_ms + 1, None, "server.connected", "{}"),
    )
    conn.commit()
    rows = conn.execute(
        "SELECT event_type, oc_session_id FROM event_log ORDER BY id"
    ).fetchall()
    assert [r["event_type"] for r in rows] == [
        "message.part.delta",
        "server.connected",
    ]
    assert rows[1]["oc_session_id"] is None


# ---------------------------------------------------------------------------
# Foreign-key enforcement
# ---------------------------------------------------------------------------


def test_surface_binding_to_unknown_user_rejected(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO surface_bindings "
            "(user_id, surface, surface_id) "
            "VALUES (?, ?, ?)",
            (999, "telegram", "x"),
        )
        conn.commit()


def test_channel_to_unknown_user_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO channels "
            "(user_id, surface, surface_id, agent) "
            "VALUES (?, ?, ?, ?)",
            (999, "cli", "tty1", "scufris"),
        )
        conn.commit()


def test_session_link_to_unknown_channel_rejected(
    conn: sqlite3.Connection,
) -> None:
    now = int(time.time())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO session_links "
            "(channel_id, oc_session_id, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?)",
            (999, "ses_x", now, now),
        )
        conn.commit()

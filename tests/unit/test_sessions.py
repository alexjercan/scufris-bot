"""Tests for ``scufris_server.sessions`` (#10 — ``20260613-091044``).

Service-layer tests run directly against a real SQLite connection
opened via :func:`scufris_server.store.connect` so we exercise the
same WAL / row_factory / FK setup the production code uses. No
HTTP, no FastAPI — those layers are tested in
``tests/unit/test_chat_route.py`` and (forthcoming) the sessions
route file.

Coverage map
------------
- :func:`create_channel_link` — atomic INSERT pair + return value.
- :func:`get_channel`         — round-trip with create; missing-id
                                 returns ``None``.
- :func:`list_user_channels`  — empty list for empty user; sort
                                 order by ``last_used_at DESC``;
                                 scoped to ``user_id``.
- :func:`clear_channel_link`  — idempotency, channels-row
                                 preservation, missing-id returns
                                 ``False``.
- :func:`clear_user_links`    — count semantics, channels-row +
                                 other-users-link preservation,
                                 zero-on-empty idempotency.

The atomicity guarantee inside :func:`create_channel_link`
(``with conn:`` wrapping both INSERTs) is documented behaviour
but is not crash-injectable from Python without monkeypatching
sqlite3 internals; we rely on the design contract + a single
"both rows present after one call" assertion as a smoke check.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from scufris_server.config import Settings
from scufris_server.sessions import (
    clear_channel_link,
    clear_user_links,
    create_channel_link,
    get_channel,
    list_user_channels,
)
from scufris_server.store import apply_migrations, connect

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Migrated, empty SQLite connection.

    Uses :func:`scufris_server.store.connect` so PRAGMAs (WAL, FKs,
    row_factory) match production. The DB file is dropped at test
    exit when ``tmp_path`` is cleaned.
    """
    settings = Settings(state_dir=tmp_path)
    with connect(settings) as c:
        apply_migrations(c)
        yield c


@pytest.fixture
def seeded_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    """``conn`` plus two seeded users: alex (id=1), bob (id=2).

    Most session tests need at least one user (FK target on
    ``channels.user_id``); a few want two so we can prove
    user-scoping. Seeding both up front keeps test bodies focused
    on the function under test.
    """
    now = int(time.time())
    conn.execute(
        "INSERT INTO users (id, username, created_at) VALUES (?, ?, ?)",
        (1, "alex", now),
    )
    conn.execute(
        "INSERT INTO users (id, username, created_at) VALUES (?, ?, ?)",
        (2, "bob", now),
    )
    conn.commit()
    return conn


def _make_link(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    surface: str = "cli",
    surface_id: str = "tty1",
    agent: str = "scufris",
    oc_session_id: str = "ses_test",
) -> int:
    """Thin wrapper around :func:`create_channel_link` with defaults.

    Lets test bodies stay short when only one or two parameters
    actually matter for the case under test.
    """
    return create_channel_link(conn, user_id, surface, surface_id, agent, oc_session_id)


# ---------------------------------------------------------------------------
# create_channel_link
# ---------------------------------------------------------------------------


def test_create_channel_link_inserts_channel_and_link_atomically(
    seeded_conn: sqlite3.Connection,
) -> None:
    """One call writes one channel row and one link row, both keyed by
    the returned ``channel_id``."""
    before = int(time.time())
    channel_id = create_channel_link(
        seeded_conn,
        user_id=1,
        surface="cli",
        surface_id="tty1",
        agent="scufris",
        oc_session_id="ses_abc",
    )

    channel_row = seeded_conn.execute(
        "SELECT user_id, surface, surface_id, agent FROM channels WHERE id = ?",
        (channel_id,),
    ).fetchone()
    assert channel_row is not None
    assert channel_row["user_id"] == 1
    assert channel_row["surface"] == "cli"
    assert channel_row["surface_id"] == "tty1"
    assert channel_row["agent"] == "scufris"

    link_row = seeded_conn.execute(
        "SELECT oc_session_id, created_at, last_used_at "
        "FROM session_links WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    assert link_row is not None
    assert link_row["oc_session_id"] == "ses_abc"
    # created_at and last_used_at are set to "now" by the function;
    # loose >= bound only — clock can advance during the call.
    assert link_row["created_at"] >= before
    assert link_row["last_used_at"] == link_row["created_at"]


def test_create_channel_link_relinks_orphan_channel_after_clear(
    seeded_conn: sqlite3.Connection,
) -> None:
    """After a clear, a second create on the same channel tuple
    reuses the existing ``channels.id`` instead of failing the
    UNIQUE constraint.

    Regression test for the orphan-channel bug surfaced by step 8
    of #11 — a curl against an existing channel-after-clear would
    hit ``sqlite3.IntegrityError: UNIQUE constraint failed:
    channels.user_id, channels.surface, channels.surface_id,
    channels.agent`` because the route always treated
    ``_resolve_session → None`` as "first-ever chat". The fix
    detects orphan ``channels`` rows in
    :func:`create_channel_link` and INSERTs only the missing
    ``session_links`` row.

    Asserts: (1) the relink call succeeds; (2) the returned
    ``channel_id`` equals the original; (3) the channels row was
    *not* duplicated; (4) the new ``session_links`` row carries
    the new ``oc_session_id`` (not the cleared one).
    """
    first = create_channel_link(
        seeded_conn,
        user_id=1,
        surface="cli",
        surface_id="alex",
        agent="build",
        oc_session_id="ses_original",
    )
    cleared = clear_channel_link(seeded_conn, first)
    assert cleared is True

    second = create_channel_link(
        seeded_conn,
        user_id=1,
        surface="cli",
        surface_id="alex",
        agent="build",
        oc_session_id="ses_fresh",
    )

    # Same channel id: relink, not a fresh row.
    assert second == first

    # Exactly one channels row for the tuple — UNIQUE held, no
    # duplicate snuck in.
    channel_count = seeded_conn.execute(
        "SELECT COUNT(*) AS n FROM channels "
        "WHERE user_id=? AND surface=? AND surface_id=? AND agent=?",
        (1, "cli", "alex", "build"),
    ).fetchone()
    assert channel_count["n"] == 1

    # The link points at the *new* oc_session_id, not the cleared one.
    link_row = seeded_conn.execute(
        "SELECT oc_session_id FROM session_links WHERE channel_id = ?",
        (second,),
    ).fetchone()
    assert link_row is not None
    assert link_row["oc_session_id"] == "ses_fresh"


# ---------------------------------------------------------------------------
# get_channel
# ---------------------------------------------------------------------------


def test_get_channel_round_trips_through_create(
    seeded_conn: sqlite3.Connection,
) -> None:
    """ChannelRow.* matches the values passed into create_channel_link."""
    channel_id = create_channel_link(
        seeded_conn,
        user_id=1,
        surface="telegram",
        surface_id="chat_42",
        agent="scufris",
        oc_session_id="ses_xyz",
    )

    fetched = get_channel(seeded_conn, channel_id)
    assert fetched is not None
    assert fetched.channel_id == channel_id
    assert fetched.user_id == 1
    assert fetched.surface == "telegram"
    assert fetched.surface_id == "chat_42"
    assert fetched.agent == "scufris"
    assert fetched.oc_session_id == "ses_xyz"
    assert fetched.created_at == fetched.last_used_at  # fresh link


def test_get_channel_returns_none_for_unknown_id(
    seeded_conn: sqlite3.Connection,
) -> None:
    """Unknown ``channel_id`` → ``None`` (not an exception)."""
    assert get_channel(seeded_conn, 9999) is None


# ---------------------------------------------------------------------------
# list_user_channels
# ---------------------------------------------------------------------------


def test_list_user_channels_empty_when_user_has_no_channels(
    seeded_conn: sqlite3.Connection,
) -> None:
    """Existing user, no channels → empty list (not None, not 404)."""
    assert list_user_channels(seeded_conn, user_id=1) == []


def test_list_user_channels_orders_by_last_used_at_desc(
    seeded_conn: sqlite3.Connection,
) -> None:
    """Three channels with hand-tuned ``last_used_at`` come back
    most-recent first (D5 ordering, resolves Q3)."""
    a = _make_link(seeded_conn, 1, surface_id="a", oc_session_id="ses_a")
    b = _make_link(seeded_conn, 1, surface_id="b", oc_session_id="ses_b")
    c = _make_link(seeded_conn, 1, surface_id="c", oc_session_id="ses_c")

    # Stamp distinct last_used_at values out of insert order so we
    # know the SQL ORDER BY (not ROWID) is what governs the result.
    # Higher number == more recent.
    seeded_conn.execute(
        "UPDATE session_links SET last_used_at = ? WHERE channel_id = ?",
        (1000, a),
    )
    seeded_conn.execute(
        "UPDATE session_links SET last_used_at = ? WHERE channel_id = ?",
        (3000, b),
    )
    seeded_conn.execute(
        "UPDATE session_links SET last_used_at = ? WHERE channel_id = ?",
        (2000, c),
    )
    seeded_conn.commit()

    listed = list_user_channels(seeded_conn, user_id=1)
    assert [r.channel_id for r in listed] == [b, c, a]
    assert [r.last_used_at for r in listed] == [3000, 2000, 1000]


def test_list_user_channels_scopes_by_user_id(
    seeded_conn: sqlite3.Connection,
) -> None:
    """alex's list contains only alex's channels; bob's the same.

    Proves the WHERE c.user_id = ? clause isn't accidentally a
    WHERE TRUE.
    """
    alex_a = _make_link(seeded_conn, 1, surface_id="a1", oc_session_id="ses_alex_a")
    alex_b = _make_link(seeded_conn, 1, surface_id="a2", oc_session_id="ses_alex_b")
    bob_a = _make_link(seeded_conn, 2, surface_id="b1", oc_session_id="ses_bob_a")

    alex_listed = {r.channel_id for r in list_user_channels(seeded_conn, 1)}
    bob_listed = {r.channel_id for r in list_user_channels(seeded_conn, 2)}

    assert alex_listed == {alex_a, alex_b}
    assert bob_listed == {bob_a}


# ---------------------------------------------------------------------------
# clear_channel_link
# ---------------------------------------------------------------------------


def test_clear_channel_link_returns_true_then_false_on_repeat(
    seeded_conn: sqlite3.Connection,
) -> None:
    """First clear deletes the link (True); second is a no-op (False).

    Idempotency contract: callers can retry safely.
    """
    channel_id = _make_link(seeded_conn, 1)

    assert clear_channel_link(seeded_conn, channel_id) is True
    assert clear_channel_link(seeded_conn, channel_id) is False


def test_clear_channel_link_preserves_channels_row(
    seeded_conn: sqlite3.Connection,
) -> None:
    """ADR-13 / §9.4 — channels row survives the clear; only the
    session_link is destroyed."""
    channel_id = _make_link(seeded_conn, 1)

    clear_channel_link(seeded_conn, channel_id)

    channel_row = seeded_conn.execute(
        "SELECT id, user_id FROM channels WHERE id = ?", (channel_id,)
    ).fetchone()
    assert channel_row is not None
    assert channel_row["user_id"] == 1

    link_row = seeded_conn.execute(
        "SELECT 1 FROM session_links WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    assert link_row is None

    # And get_channel — which inner-joins — now reports orphan as
    # invisible (Joining policy).
    assert get_channel(seeded_conn, channel_id) is None


def test_clear_channel_link_returns_false_for_unknown_channel_id(
    seeded_conn: sqlite3.Connection,
) -> None:
    """Channel id that never existed → False, no side effects."""
    assert clear_channel_link(seeded_conn, 9999) is False


# ---------------------------------------------------------------------------
# clear_user_links
# ---------------------------------------------------------------------------


def test_clear_user_links_returns_count_and_preserves_other_users(
    seeded_conn: sqlite3.Connection,
) -> None:
    """Bulk clear deletes only the target user's links; channels rows
    survive on both sides; the other user's link is untouched."""
    alex_a = _make_link(seeded_conn, 1, surface_id="a1", oc_session_id="ses_alex_a")
    alex_b = _make_link(seeded_conn, 1, surface_id="a2", oc_session_id="ses_alex_b")
    alex_c = _make_link(seeded_conn, 1, surface_id="a3", oc_session_id="ses_alex_c")
    bob_a = _make_link(seeded_conn, 2, surface_id="b1", oc_session_id="ses_bob_a")

    count = clear_user_links(seeded_conn, user_id=1)
    assert count == 3

    # alex's channel rows survive.
    alex_channel_count = seeded_conn.execute(
        "SELECT COUNT(*) AS n FROM channels WHERE user_id = 1"
    ).fetchone()["n"]
    assert alex_channel_count == 3

    # alex's links are gone.
    alex_link_count = seeded_conn.execute(
        "SELECT COUNT(*) AS n FROM session_links WHERE channel_id IN (?, ?, ?)",
        (alex_a, alex_b, alex_c),
    ).fetchone()["n"]
    assert alex_link_count == 0

    # bob's link is untouched.
    bob_link = seeded_conn.execute(
        "SELECT oc_session_id FROM session_links WHERE channel_id = ?",
        (bob_a,),
    ).fetchone()
    assert bob_link is not None
    assert bob_link["oc_session_id"] == "ses_bob_a"


def test_clear_user_links_returns_zero_when_user_has_no_links(
    seeded_conn: sqlite3.Connection,
) -> None:
    """No links to clear → 0; second call after a real clear → 0
    (idempotency)."""
    # Cold: never had any links.
    assert clear_user_links(seeded_conn, user_id=1) == 0

    # After making + clearing, a third call still returns 0.
    _make_link(seeded_conn, 1)
    assert clear_user_links(seeded_conn, user_id=1) == 1
    assert clear_user_links(seeded_conn, user_id=1) == 0


# ---------------------------------------------------------------------------
# ChannelRow shape
# ---------------------------------------------------------------------------


def test_channel_row_is_frozen(seeded_conn: sqlite3.Connection) -> None:
    """ChannelRow is a snapshot, not a handle — assignment must raise.

    Documents the dataclass(frozen=True) contract so future edits
    don't silently relax it.
    """
    channel_id = _make_link(seeded_conn, 1)
    row = get_channel(seeded_conn, channel_id)
    assert row is not None

    with pytest.raises((AttributeError, TypeError)):
        # ``dataclasses.FrozenInstanceError`` subclasses AttributeError;
        # accept TypeError too in case Python tightens it.
        row.user_id = 99  # type: ignore[misc]


def test_channel_row_equality_is_value_based(
    seeded_conn: sqlite3.Connection,
) -> None:
    """Two ChannelRow snapshots from the same DB row compare equal.

    Confirms @dataclass auto-generated __eq__ is in effect; lets
    callers/tests use ``==`` instead of field-by-field assertions.
    """
    channel_id = _make_link(seeded_conn, 1)
    first = get_channel(seeded_conn, channel_id)
    second = get_channel(seeded_conn, channel_id)
    assert first == second
    assert first is not second  # distinct instances

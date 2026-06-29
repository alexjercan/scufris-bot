"""Tests for ``scufris_server.identity`` (#12).

Two groups:

1. **TOML loading** — :func:`load_user_identity` against a battery
   of well-formed and malformed config files; covers the four "what
   happens when…" scenarios from TASK.md (missing file, malformed
   TOML, schema violation, extra keys).

2. **Resolution** — :func:`resolve_user` against an in-memory SQLite
   db with the production schema. Covers all four resolution paths
   (override, cache, TOML hit, default fallback) plus idempotency
   and the "stickiness over TOML edits" property.

We exercise the public API exclusively. Internal helpers
(``_ensure_user_row``, ``_ensure_surface_binding``, ``_bound_surfaces``)
are covered transitively.
"""

from __future__ import annotations

import sqlite3
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from scufris_server.config import Settings
from scufris_server.identity import (
    DEFAULT_USER_ID,
    DEFAULT_USERNAME,
    BoundSurface,
    IdentityFile,
    UserConfig,
    _xdg_config_path,
    load_user_identity,
    resolve_user,
)
from scufris_server.store import apply_migrations, connect

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """In-memory-style SQLite + schema + seeded default user.

    Uses :func:`scufris_server.store.connect` so the test exercises
    the same WAL / row_factory setup production does. The DB file
    lives under ``tmp_path`` and is dropped at test exit.
    """
    settings = Settings(state_dir=tmp_path)
    with connect(settings) as c:
        apply_migrations(c)
        # Seed the default user — production does this via
        # app._seed_default_user during lifespan.
        c.execute(
            "INSERT OR IGNORE INTO users (id, username, created_at) VALUES (?, ?, ?)",
            (DEFAULT_USER_ID, DEFAULT_USERNAME, int(time.time())),
        )
        c.commit()
        yield c


@pytest.fixture
def empty_identity() -> IdentityFile:
    """An :class:`IdentityFile` with no user defined."""
    return IdentityFile()


@pytest.fixture
def alex_identity() -> IdentityFile:
    """An :class:`IdentityFile` with one user 'alex' bound to cli + telegram."""
    return IdentityFile(
        user=UserConfig(
            username="alex",
            identity={"cli": "alex", "telegram": "8231376426"},
        )
    )


def _write_toml(path: Path, body: str) -> Path:
    """Write ``body`` to ``path`` and return ``path``. Creates parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _xdg_config_path
# ---------------------------------------------------------------------------


def test_xdg_config_path_uses_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert _xdg_config_path() == tmp_path / "scufris" / "config.toml"


def test_xdg_config_path_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert _xdg_config_path() == Path.home() / ".config" / "scufris" / "config.toml"


# ---------------------------------------------------------------------------
# load_user_identity — happy paths
# ---------------------------------------------------------------------------


def test_load_user_identity_missing_file_returns_empty(tmp_path: Path) -> None:
    target = tmp_path / "missing.toml"
    result = load_user_identity(target)
    assert result.user is None


def test_load_user_identity_valid_full(tmp_path: Path) -> None:
    target = _write_toml(
        tmp_path / "config.toml",
        '[user]\nusername = "alex"\n\n'
        '[user.identity]\ncli = "alex"\ntelegram = "8231376426"\n',
    )
    result = load_user_identity(target)
    assert result.user is not None
    assert result.user.username == "alex"
    assert result.user.identity == {"cli": "alex", "telegram": "8231376426"}


def test_load_user_identity_no_identity_section(tmp_path: Path) -> None:
    """``[user]`` present without ``[user.identity]`` → empty identity dict."""
    target = _write_toml(tmp_path / "config.toml", '[user]\nusername = "alex"\n')
    result = load_user_identity(target)
    assert result.user is not None
    assert result.user.username == "alex"
    assert result.user.identity == {}


def test_load_user_identity_explicit_path_wins_over_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``path=`` arg bypasses XDG resolution."""
    # Set XDG to a directory that does NOT contain config.toml.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    explicit = _write_toml(tmp_path / "elsewhere.toml", '[user]\nusername = "bob"\n')
    result = load_user_identity(explicit)
    assert result.user is not None
    assert result.user.username == "bob"


def test_load_user_identity_xdg_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``path`` is None, XDG_CONFIG_HOME is consulted."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_toml(
        tmp_path / "scufris" / "config.toml",
        '[user]\nusername = "via_xdg"\n',
    )
    result = load_user_identity(None)
    assert result.user is not None
    assert result.user.username == "via_xdg"


# ---------------------------------------------------------------------------
# load_user_identity — error paths
# ---------------------------------------------------------------------------


def test_load_user_identity_malformed_raises(tmp_path: Path) -> None:
    target = _write_toml(tmp_path / "bad.toml", "this is = not valid toml [\n")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_user_identity(target)


def test_load_user_identity_missing_username_raises(tmp_path: Path) -> None:
    """``[user]`` table present but ``username`` field missing."""
    target = _write_toml(
        tmp_path / "config.toml",
        '[user]\n[user.identity]\ncli = "alex"\n',
    )
    with pytest.raises(ValidationError):
        load_user_identity(target)


def test_load_user_identity_non_string_surface_id_raises(tmp_path: Path) -> None:
    """``identity`` values must be strings; ints fail validation."""
    target = _write_toml(
        tmp_path / "config.toml",
        '[user]\nusername = "alex"\n[user.identity]\ntelegram = 8231376426\n',
    )
    with pytest.raises(ValidationError):
        load_user_identity(target)


# ---------------------------------------------------------------------------
# load_user_identity — leniency (extras ignored)
# ---------------------------------------------------------------------------


def test_load_user_identity_extra_top_level_keys_ignored(tmp_path: Path) -> None:
    """``[server]`` (and anything else outside ``[user]``) is dropped."""
    target = _write_toml(
        tmp_path / "config.toml",
        '[user]\nusername = "alex"\n\n[server]\nport = 9999\nbind = "0.0.0.0"\n',
    )
    result = load_user_identity(target)
    assert result.user is not None
    assert result.user.username == "alex"
    # Server fields aren't on the model; nothing crashes.


def test_load_user_identity_extra_subtables_ignored(tmp_path: Path) -> None:
    """``[user.schedule]`` and friends are tolerated for v1 carryover."""
    target = _write_toml(
        tmp_path / "config.toml",
        '[user]\nusername = "alex"\ntimezone = "Europe/Bucharest"\n\n'
        "[user.schedule]\nenabled = true\n\n"
        "[user.rag]\nsources = []\n\n"
        "[user.notifications]\ndesktop = true\n",
    )
    result = load_user_identity(target)
    assert result.user is not None
    assert result.user.username == "alex"
    assert result.user.identity == {}


def test_load_user_identity_v1_example_file(tmp_path: Path) -> None:
    """Acceptance criterion: drop a v1 config.toml in, v2 reads it.

    Mirrors the example from ``tasks/20260520-145231/TASK.md`` lines
    60–101 — full v1 config.toml. We assert only on the [user] +
    [user.identity] subset; the rest must not error.
    """
    target = _write_toml(
        tmp_path / "config.toml",
        """
[user]
username = "alex"
timezone = "Europe/Bucharest"

[user.identity]
cli = "alex"
telegram = "8231376426"

[user.schedule]
enabled = true

[[user.schedule.slots]]
name = "morning"
time = "07:30"
days = ["mon", "tue", "wed", "thu", "fri"]
briefing = "morning"
surfaces = ["telegram"]

[user.rag]
sources = [
  { name = "journal", path = "~/journal", type = "markdown", watch = true },
]

[user.journal]
den_path = "~/journal"

[user.notifications]
desktop = true
telegram = true
""",
    )
    result = load_user_identity(target)
    assert result.user is not None
    assert result.user.username == "alex"
    assert result.user.identity == {"cli": "alex", "telegram": "8231376426"}


# ---------------------------------------------------------------------------
# resolve_user — override path (D4)
# ---------------------------------------------------------------------------


def test_resolve_user_override_returns_pinned(
    conn: sqlite3.Connection, alex_identity: IdentityFile
) -> None:
    # Insert a second user row to override against.
    conn.execute(
        "INSERT INTO users (id, username, created_at) VALUES (?, ?, ?)",
        (42, "pinned", int(time.time())),
    )
    conn.commit()

    resolved = resolve_user(
        conn,
        surface="cli",
        surface_id="anything",
        identity_file=alex_identity,
        override_user_id=42,
    )
    assert resolved.user_id == 42
    assert resolved.username == "pinned"
    # No binding side-effect under override.
    rows = conn.execute("SELECT COUNT(*) AS n FROM surface_bindings").fetchone()
    assert rows["n"] == 0


def test_resolve_user_override_missing_user_raises(
    conn: sqlite3.Connection, empty_identity: IdentityFile
) -> None:
    with pytest.raises(RuntimeError, match="SCUFRIS_USER_ID override"):
        resolve_user(
            conn,
            surface="cli",
            surface_id="alex",
            identity_file=empty_identity,
            override_user_id=999,
        )


def test_resolve_user_override_includes_bound_surfaces_for_pinned_user(
    conn: sqlite3.Connection, empty_identity: IdentityFile
) -> None:
    """Override response still surfaces the pinned user's existing bindings."""
    conn.execute(
        "INSERT INTO users (id, username, created_at) VALUES (?, ?, ?)",
        (5, "pinned", int(time.time())),
    )
    conn.execute(
        "INSERT INTO surface_bindings (user_id, surface, surface_id) VALUES (?, ?, ?)",
        (5, "telegram", "111"),
    )
    conn.commit()

    resolved = resolve_user(
        conn,
        surface="cli",
        surface_id="dev",
        identity_file=empty_identity,
        override_user_id=5,
    )
    assert resolved.bound_surfaces == [
        BoundSurface(surface="telegram", surface_id="111")
    ]


# ---------------------------------------------------------------------------
# resolve_user — cached binding (step 2)
# ---------------------------------------------------------------------------


def test_resolve_user_cached_binding_returns_existing(
    conn: sqlite3.Connection, empty_identity: IdentityFile
) -> None:
    conn.execute(
        "INSERT INTO users (id, username, created_at) VALUES (?, ?, ?)",
        (7, "preexisting", int(time.time())),
    )
    conn.execute(
        "INSERT INTO surface_bindings (user_id, surface, surface_id) VALUES (?, ?, ?)",
        (7, "cli", "preexisting"),
    )
    conn.commit()

    resolved = resolve_user(conn, "cli", "preexisting", empty_identity)
    assert resolved.user_id == 7
    assert resolved.username == "preexisting"
    # Idempotent: no new row inserted.
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM surface_bindings").fetchone()["n"] == 1
    )


def test_resolve_user_cached_takes_precedence_over_toml(
    conn: sqlite3.Connection,
) -> None:
    """Once a binding exists, TOML changes don't re-route it (stickiness).

    Scenario: (cli, alex) was originally bound to user "first"; later
    the operator edits TOML to map cli=alex under a new user "second".
    Resolve must keep returning "first" — the DB is sticky.
    """
    conn.execute(
        "INSERT INTO users (id, username, created_at) VALUES (?, ?, ?)",
        (10, "first", int(time.time())),
    )
    conn.execute(
        "INSERT INTO surface_bindings (user_id, surface, surface_id) VALUES (?, ?, ?)",
        (10, "cli", "alex"),
    )
    conn.commit()

    toml_says_second = IdentityFile(
        user=UserConfig(username="second", identity={"cli": "alex"})
    )
    resolved = resolve_user(conn, "cli", "alex", toml_says_second)
    assert resolved.user_id == 10
    assert resolved.username == "first"


# Note: the "binding points at non-existent user_id" RuntimeError branch
# in resolve_user can't be tested through the public API — the schema's
# FOREIGN KEY (user_id) REFERENCES users(id) constraint prevents the
# orphan from existing in the first place. The same defensive
# _username_for(None) branch is covered by
# test_resolve_user_override_missing_user_raises above (override path).


# ---------------------------------------------------------------------------
# resolve_user — TOML hit (step 3)
# ---------------------------------------------------------------------------


def test_resolve_user_toml_hit_creates_user_and_binding(
    conn: sqlite3.Connection, alex_identity: IdentityFile
) -> None:
    resolved = resolve_user(conn, "cli", "alex", alex_identity)
    assert resolved.username == "alex"
    assert resolved.user_id != DEFAULT_USER_ID  # new row, not the default

    # Verify side effects in DB.
    row = conn.execute("SELECT id FROM users WHERE username = 'alex'").fetchone()
    assert row is not None
    assert int(row["id"]) == resolved.user_id

    binding = conn.execute(
        "SELECT user_id FROM surface_bindings WHERE surface = ? AND surface_id = ?",
        ("cli", "alex"),
    ).fetchone()
    assert binding is not None
    assert int(binding["user_id"]) == resolved.user_id


def test_resolve_user_toml_hit_existing_user_row_reused(
    conn: sqlite3.Connection, alex_identity: IdentityFile
) -> None:
    """alex's row already exists from a prior boot; TOML hit binds without
    re-inserting it."""
    conn.execute(
        "INSERT INTO users (id, username, created_at) VALUES (?, ?, ?)",
        (33, "alex", int(time.time())),
    )
    conn.commit()

    resolved = resolve_user(conn, "cli", "alex", alex_identity)
    assert resolved.user_id == 33

    # Only one alex row.
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE username = 'alex'"
    ).fetchone()["n"]
    assert n == 1


def test_resolve_user_toml_miss_falls_through_to_default(
    conn: sqlite3.Connection, alex_identity: IdentityFile
) -> None:
    """alex's TOML lists ``cli=alex``; ``cli=stranger`` doesn't match."""
    resolved = resolve_user(conn, "cli", "stranger", alex_identity)
    assert resolved.user_id == DEFAULT_USER_ID
    assert resolved.username == DEFAULT_USERNAME


# ---------------------------------------------------------------------------
# resolve_user — default fallback (step 4)
# ---------------------------------------------------------------------------


def test_resolve_user_default_fallback_creates_binding(
    conn: sqlite3.Connection, empty_identity: IdentityFile
) -> None:
    resolved = resolve_user(conn, "cli", "newcomer", empty_identity)
    assert resolved.user_id == DEFAULT_USER_ID
    assert resolved.username == DEFAULT_USERNAME

    binding = conn.execute(
        "SELECT user_id FROM surface_bindings WHERE surface = 'cli' AND surface_id = 'newcomer'"
    ).fetchone()
    assert binding is not None
    assert int(binding["user_id"]) == DEFAULT_USER_ID


def test_resolve_user_default_user_missing_raises(tmp_path: Path) -> None:
    """Empty users table → fallback can't resolve to a username → RuntimeError."""
    settings = Settings(state_dir=tmp_path)
    with connect(settings) as c:
        apply_migrations(c)
        # NB: skip seeding the default user.
        with pytest.raises(RuntimeError, match="default user"):
            resolve_user(c, "cli", "anyone", IdentityFile())


# ---------------------------------------------------------------------------
# resolve_user — idempotency + bound_surfaces aggregation
# ---------------------------------------------------------------------------


def test_resolve_user_idempotent(
    conn: sqlite3.Connection, alex_identity: IdentityFile
) -> None:
    """Two back-to-back resolves return the same row, with no duplicate
    surface_bindings entry."""
    first = resolve_user(conn, "cli", "alex", alex_identity)
    second = resolve_user(conn, "cli", "alex", alex_identity)
    assert first == second

    n = conn.execute(
        "SELECT COUNT(*) AS n FROM surface_bindings "
        "WHERE surface = 'cli' AND surface_id = 'alex'"
    ).fetchone()["n"]
    assert n == 1


def test_resolve_user_bound_surfaces_aggregated_and_sorted(
    conn: sqlite3.Connection, alex_identity: IdentityFile
) -> None:
    """resolve_user returns a snapshot of *all* bindings for the user,
    sorted (surface, surface_id) ASC."""
    # Bind alex on both cli and telegram.
    resolve_user(conn, "telegram", "8231376426", alex_identity)
    resolved = resolve_user(conn, "cli", "alex", alex_identity)

    assert resolved.bound_surfaces == [
        BoundSurface(surface="cli", surface_id="alex"),
        BoundSurface(surface="telegram", surface_id="8231376426"),
    ]


def test_resolve_user_bound_surfaces_for_default_includes_new_binding(
    conn: sqlite3.Connection, empty_identity: IdentityFile
) -> None:
    """A first-time default-fallback resolve includes the row it just wrote."""
    resolved = resolve_user(conn, "cli", "newcomer", empty_identity)
    assert resolved.bound_surfaces == [
        BoundSurface(surface="cli", surface_id="newcomer")
    ]

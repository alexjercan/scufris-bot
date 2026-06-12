"""Tests for :mod:`utils.session_store` and the :class:`AgentManager`
integration that persists ``user_id -> session_id`` across restarts.

Covers:

- Empty store on missing file (no error).
- Round-trip via a fresh :class:`SessionStore` pointing at the same
  path -- the "server restart" scenario.
- Atomic-rename: no ``.tmp`` files lingering after a write.
- Corrupt JSON / wrong schema version: load returns empty and logs.
- Path resolution: ``SCUFRIS_DATA_DIR`` beats ``STATE_DIRECTORY``
  beats the repo-relative fallback.
- :meth:`AgentManager.get_or_create_session` writes through to disk.
- :meth:`AgentManager.delete_session` removes from disk.
- :meth:`AgentManager.prune_invalid_sessions` drops upstream-missing
  ids, keeps live ones, and survives ``list_sessions()`` failure.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from utils.session_store import (
    DEFAULT_FILENAME,
    SCHEMA_VERSION,
    SessionStore,
    _default_data_dir,
    default_session_store_path,
)

# ---------------------------------------------------------------------------
# SessionStore — pure-disk behaviour
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    assert store.as_dict() == {}
    # No file on disk is the right state when nothing's been written.
    assert not (tmp_path / "sessions.json").exists()


def test_set_and_get_persists_and_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store = SessionStore(path)

    store.set(42, "ses_abc")
    store.set(7, "ses_xyz")

    # In-memory state.
    assert store.get(42) == "ses_abc"
    assert store.as_dict() == {42: "ses_abc", 7: "ses_xyz"}

    # On-disk state.
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "version": SCHEMA_VERSION,
        "sessions": {"7": "ses_xyz", "42": "ses_abc"},
    }


def test_restart_simulation_reloads_persisted_entries(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"

    first = SessionStore(path)
    first.set(42, "ses_abc")
    first.set(7, "ses_xyz")
    assert first.as_dict() == {42: "ses_abc", 7: "ses_xyz"}

    # "Restart" — second instance reads the file fresh.
    second = SessionStore(path)
    assert second.as_dict() == {42: "ses_abc", 7: "ses_xyz"}


def test_set_same_value_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    store.set(42, "ses_abc")
    mtime1 = path.stat().st_mtime_ns

    # Repeated set with same value should not rewrite the file.
    store.set(42, "ses_abc")
    mtime2 = path.stat().st_mtime_ns
    assert mtime1 == mtime2


def test_set_rejects_empty_session_id(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    with pytest.raises(ValueError):
        store.set(42, "")
    with pytest.raises(ValueError):
        store.set(42, None)  # type: ignore[arg-type]


def test_pop_removes_entry_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    store.set(42, "ses_abc")
    store.set(7, "ses_xyz")

    removed = store.pop(42)
    assert removed == "ses_abc"
    assert store.as_dict() == {7: "ses_xyz"}

    # Reload to confirm the disk side too.
    reloaded = SessionStore(path)
    assert reloaded.as_dict() == {7: "ses_xyz"}


def test_pop_missing_returns_none(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    assert store.pop(99) is None


def test_replace_all_atomic(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    store.set(1, "ses_1")
    store.set(2, "ses_2")
    store.set(3, "ses_3")

    store.replace_all({2: "ses_2", 4: "ses_4"})
    assert store.as_dict() == {2: "ses_2", 4: "ses_4"}
    reloaded = SessionStore(path)
    assert reloaded.as_dict() == {2: "ses_2", 4: "ses_4"}


def test_no_tmp_file_lingering_after_write(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    store.set(42, "ses_abc")
    leftovers = [
        p.name for p in tmp_path.iterdir() if p.name.startswith(".opencode_sessions.")
    ]
    assert leftovers == [], leftovers


def test_corrupt_json_loads_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "sessions.json"
    path.write_text("{not valid json}", encoding="utf-8")
    with caplog.at_level("WARNING"):
        store = SessionStore(path)
    assert store.as_dict() == {}
    assert any("failed to read" in rec.message for rec in caplog.records)


def test_wrong_schema_version_loads_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "sessions.json"
    path.write_text(
        json.dumps({"version": 999, "sessions": {"1": "ses_1"}}),
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        store = SessionStore(path)
    assert store.as_dict() == {}


def test_non_object_root_loads_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "sessions.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with caplog.at_level("WARNING"):
        store = SessionStore(path)
    assert store.as_dict() == {}


def test_drops_bad_entries_during_load(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "sessions.json"
    path.write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "sessions": {
                    "42": "ses_good",
                    "not-an-int": "ses_bad_key",
                    "7": "",  # empty session_id
                    "9": 123,  # non-string session_id
                },
            }
        ),
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        store = SessionStore(path)
    assert store.as_dict() == {42: "ses_good"}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_default_path_uses_scufris_data_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCUFRIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_DIRECTORY", "/should/be/ignored")
    assert _default_data_dir() == tmp_path
    assert default_session_store_path() == tmp_path / DEFAULT_FILENAME


def test_default_path_uses_state_directory_when_no_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCUFRIS_DATA_DIR", raising=False)
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    assert _default_data_dir() == tmp_path


def test_default_path_handles_state_directory_colon_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCUFRIS_DATA_DIR", raising=False)
    monkeypatch.setenv("STATE_DIRECTORY", f"{tmp_path}:/some/other/dir")
    assert _default_data_dir() == tmp_path


def test_default_path_falls_back_to_repo_data_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCUFRIS_DATA_DIR", raising=False)
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    repo_data = Path(__file__).resolve().parent.parent / "data"
    assert _default_data_dir() == repo_data


def test_constructor_uses_default_path_when_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCUFRIS_DATA_DIR", str(tmp_path))
    store = SessionStore()
    assert store.path == tmp_path / DEFAULT_FILENAME


def test_save_creates_parent_directory(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "sessions.json"
    store = SessionStore(deep)
    store.set(42, "ses_abc")
    assert deep.exists()


# ---------------------------------------------------------------------------
# AgentManager integration
# ---------------------------------------------------------------------------


class _StubOpenCodeClient:
    """Minimal stand-in for :class:`OpenCodeClient`.

    Tracks created sessions so tests can assert which calls landed.
    """

    def __init__(
        self,
        *,
        upstream: List[Dict[str, Any]] | None = None,
        list_raises: BaseException | None = None,
    ) -> None:
        self._counter = 0
        self.created: List[str] = []
        self.deleted: List[str] = []
        self._upstream = list(upstream) if upstream is not None else []
        self._list_raises = list_raises

    async def create_session(self) -> Dict[str, Any]:
        self._counter += 1
        sid = f"ses_new_{self._counter}"
        self.created.append(sid)
        # Mirror the upstream "GET /session" view so a subsequent
        # prune does not drop just-created entries.
        self._upstream.append({"id": sid})
        return {"id": sid}

    async def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)
        self._upstream = [s for s in self._upstream if s.get("id") != session_id]

    async def list_sessions(self) -> List[Dict[str, Any]]:
        if self._list_raises is not None:
            raise self._list_raises
        return list(self._upstream)


def _build_agent(store: SessionStore, client: _StubOpenCodeClient):  # type: ignore[no-untyped-def]
    """Construct an :class:`AgentManager` with the stub client. Late
    import avoids loading the heavy module at collection time."""
    from utils.agent import AgentManager

    return AgentManager(client, session_store=store)  # type: ignore[arg-type]


def test_agent_get_or_create_writes_through_to_store(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    client = _StubOpenCodeClient()
    agent = _build_agent(store, client)

    sid = asyncio.run(agent.get_or_create_session(42))
    assert sid == "ses_new_1"
    assert store.as_dict() == {42: "ses_new_1"}

    # Reload -> entry survived the "restart".
    reloaded = SessionStore(path)
    assert reloaded.as_dict() == {42: "ses_new_1"}


def test_agent_restart_recovers_session_without_creating_new_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.json"

    # First instance: creates and persists.
    store_a = SessionStore(path)
    client_a = _StubOpenCodeClient()
    agent_a = _build_agent(store_a, client_a)
    sid_a = asyncio.run(agent_a.get_or_create_session(42))
    assert client_a.created == [sid_a]

    # Second instance ("restart"): same path, fresh client. Should
    # reuse the persisted id and NOT create a new session.
    store_b = SessionStore(path)
    client_b = _StubOpenCodeClient(upstream=[{"id": sid_a}])
    agent_b = _build_agent(store_b, client_b)
    sid_b = asyncio.run(agent_b.get_or_create_session(42))
    assert sid_b == sid_a
    assert client_b.created == []


def test_agent_delete_session_removes_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    client = _StubOpenCodeClient()
    agent = _build_agent(store, client)

    sid = asyncio.run(agent.get_or_create_session(42))
    assert store.as_dict() == {42: sid}

    deleted = asyncio.run(agent.delete_session(42))
    assert deleted == sid
    assert client.deleted == [sid]
    assert store.as_dict() == {}

    # On-disk too.
    reloaded = SessionStore(path)
    assert reloaded.as_dict() == {}


def test_agent_delete_session_missing_user_is_noop(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    client = _StubOpenCodeClient()
    agent = _build_agent(store, client)

    result = asyncio.run(agent.delete_session(404))
    assert result is None
    assert client.deleted == []


def test_prune_invalid_sessions_drops_upstream_missing(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    # Seed two persisted entries and have upstream report only one.
    store.set(1, "ses_live")
    store.set(2, "ses_dead")

    client = _StubOpenCodeClient(upstream=[{"id": "ses_live"}])
    agent = _build_agent(store, client)
    pruned = asyncio.run(agent.prune_invalid_sessions())
    assert pruned == 1
    assert store.as_dict() == {1: "ses_live"}
    reloaded = SessionStore(path)
    assert reloaded.as_dict() == {1: "ses_live"}


def test_prune_invalid_sessions_no_op_when_empty(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    client = _StubOpenCodeClient()
    agent = _build_agent(store, client)
    pruned = asyncio.run(agent.prune_invalid_sessions())
    assert pruned == 0


def test_prune_invalid_sessions_no_op_when_all_live(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    store.set(1, "ses_live")
    store.set(2, "ses_also_live")
    client = _StubOpenCodeClient(upstream=[{"id": "ses_live"}, {"id": "ses_also_live"}])
    agent = _build_agent(store, client)
    pruned = asyncio.run(agent.prune_invalid_sessions())
    assert pruned == 0
    assert store.as_dict() == {1: "ses_live", 2: "ses_also_live"}


def test_prune_invalid_sessions_swallows_client_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    store.set(1, "ses_who_knows")
    client = _StubOpenCodeClient(list_raises=RuntimeError("opencode unreachable"))
    agent = _build_agent(store, client)
    with caplog.at_level("WARNING"):
        pruned = asyncio.run(agent.prune_invalid_sessions())
    assert pruned == 0
    # Map intact: best-effort, never drop on transient failures.
    assert store.as_dict() == {1: "ses_who_knows"}
    assert any("list_sessions() failed" in rec.message for rec in caplog.records)


def test_agent_seeded_from_persisted_store(tmp_path: Path) -> None:
    """Constructing a new AgentManager pointed at an existing store
    file (the restart case) should not call ``create_session`` for
    a user whose entry was already persisted."""
    path = tmp_path / "sessions.json"
    seed = SessionStore(path)
    seed.set(99, "ses_persisted")

    fresh_store = SessionStore(path)
    client = _StubOpenCodeClient(upstream=[{"id": "ses_persisted"}])
    agent = _build_agent(fresh_store, client)
    # No upstream call needed: id comes from disk via the store.
    sid = asyncio.run(agent.get_or_create_session(99))
    assert sid == "ses_persisted"
    assert client.created == []


def test_agent_without_store_skips_persistence(tmp_path: Path) -> None:
    """Backward compat: ``session_store=None`` works the same as before."""
    from utils.agent import AgentManager

    client = _StubOpenCodeClient()
    agent = AgentManager(client)  # type: ignore[arg-type]
    sid = asyncio.run(agent.get_or_create_session(1))
    assert sid == "ses_new_1"
    # Nothing on disk.
    assert not (tmp_path / DEFAULT_FILENAME).exists()


def test_prune_dropped_entry_actually_removed_from_disk(tmp_path: Path) -> None:
    """Regression: prune must rewrite the file, not just mutate memory."""
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    store.set(1, "ses_live")
    store.set(2, "ses_dead")
    client = _StubOpenCodeClient(upstream=[{"id": "ses_live"}])
    agent = _build_agent(store, client)
    asyncio.run(agent.prune_invalid_sessions())

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["sessions"] == {"1": "ses_live"}


# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------


def test_store_reexported_from_utils() -> None:
    """Public surface: ``from utils import SessionStore`` works."""
    from utils import SessionStore as Re

    assert Re is SessionStore


def test_default_filename_constant_is_stable() -> None:
    """If this changes, write a migration."""
    assert DEFAULT_FILENAME == "opencode_sessions.json"


def test_state_directory_empty_string_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank ``STATE_DIRECTORY`` shouldn't be honoured."""
    monkeypatch.delenv("SCUFRIS_DATA_DIR", raising=False)
    monkeypatch.setenv("STATE_DIRECTORY", "")
    repo_data = Path(__file__).resolve().parent.parent / "data"
    assert _default_data_dir() == repo_data


def test_isolation_does_not_touch_repo_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests must not poke the real ``<repo>/data`` directory."""
    monkeypatch.setenv("SCUFRIS_DATA_DIR", str(tmp_path))
    store = SessionStore()
    store.set(1, "ses_isolated")
    repo_data = Path(__file__).resolve().parent.parent / "data"
    # Either the repo dir doesn't exist (clean tree) or, if it does,
    # it doesn't contain a session for this test's tmp file.
    if repo_data.exists():
        assert os.path.realpath(store.path) != os.path.realpath(
            repo_data / DEFAULT_FILENAME
        )

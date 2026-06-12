# Persist user_id - OpenCode session_id mapping across restarts

- STATUS: CLOSED
- PRIORITY: 40
- TAGS: opencode,persistence

## Outcome

Shipped 2026-06-10. The `user_id → session_id` map now survives
`scufris-server` restarts.

### What landed

- **`utils/session_store.py` (new)** — `SessionStore` class with a
  versioned JSON on-disk format (`{"version": 1, "sessions": {...}}`)
  and atomic-rename writes (`tempfile.mkstemp` in the same dir →
  `fsync` → `os.replace`). Path resolution:
  1. `$SCUFRIS_DATA_DIR` (explicit override)
  2. `$STATE_DIRECTORY` (set by systemd's `StateDirectory=` —
     handles colon-separated lists)
  3. `<repo>/data` (dev fallback, mirrors `utils/telemetry.py`)
  Filename: `opencode_sessions.json`. Public API:
  `get / set / pop / replace_all / as_dict / path`. Thread-safe via
  an internal `threading.Lock`. Corrupt JSON, wrong schema version,
  non-object root, and individual bad entries all log a warning and
  load empty (or skip) rather than crashing.

- **`utils/agent.py`** — `AgentManager` now takes an optional
  `session_store: Optional[SessionStore]`. When supplied:
  - Constructor seeds `_sessions` from `store.as_dict()`.
  - `get_or_create_session` calls `store.set(user_id, sid)` after
    creating a new upstream session.
  - `delete_session` calls `store.pop(user_id)` under the lock.
  - The `OpenCodeStaleSessionError` retry path in `process_message`
    also pops the persisted entry.
  - New `async prune_invalid_sessions() -> int` calls
    `client.list_sessions()` and drops persisted entries whose ids
    no longer exist upstream. Failure of `list_sessions()` is
    swallowed with a warning so transient OpenCode unreachability
    can't abort startup.
  When `session_store=None` the manager behaves exactly as before
  (legacy in-memory only, used by tests that don't care).

- **`scufris_server/bootstrap.py`** — constructs a `SessionStore()`
  (default path) and threads it into `create_agent_manager`. Logs
  the resolved file path. `Runtime` dataclass gained a
  `session_store: SessionStore` field.

- **`scufris_server/app.py`** — `_lifespan` calls
  `runtime.agent_manager.prune_invalid_sessions()` once after
  `build_runtime()`. Wrapped in a broad `except` so a flaky
  upstream never aborts startup; the count of pruned entries is
  logged when non-zero.

- **`utils/__init__.py`** — re-exports `SessionStore`,
  `default_session_store_path`, and the `SESSION_STORE_FILENAME`
  constant.

- **`tests/test_session_store.py` (new, 34 tests)** —
  - Pure store: empty load, round-trip, restart simulation via a
    second `SessionStore(path)`, idempotent `set`, value validation,
    `pop` (hit / miss), `replace_all`, no `.tmp` leftovers,
    corrupt JSON, wrong schema version, non-object root, dropping
    bad entries on load, parent-dir auto-create.
  - Path resolution: `SCUFRIS_DATA_DIR` beats `STATE_DIRECTORY`,
    colon-separated `STATE_DIRECTORY`, repo-dir fallback, empty
    `STATE_DIRECTORY` ignored.
  - `AgentManager` integration via a stub `OpenCodeClient`:
    write-through on create, read-through on restart, write-through
    on delete, no-op delete for missing user, prune drops
    upstream-missing, prune no-op on empty / all-live, prune
    swallows client error, restart-with-persisted-entry skips
    `create_session`, `session_store=None` skips persistence,
    prune actually rewrites the file (regression).
  - Sanity: `from utils import SessionStore` works,
    `DEFAULT_FILENAME` constant is stable.

- **`tests/test_server.py`** — `_make_app` helper now constructs
  a tmp-path `SessionStore` for the `Runtime` dataclass (lifespan
  is skipped, so the file is never touched but the field is
  required). Added `from pathlib import Path`.

### Acceptance criteria — all met

- [x] Restarting `scufris-server` does not start a new OpenCode
      session for users who had one before the restart.
      *(verified live: same `ses_…` id reused across restart in
      `smoke_p40.py` run #2)*
- [x] Stale entries (session deleted on the OpenCode side) are
      transparently pruned and the next message creates a fresh one.
      *(verified live: bogus pre-seeded id → empty store post-prune
      → fresh `ses_…` after next chat in run #3)*
- [x] `POST /v1/clear` removes both the in-memory entry and the
      persisted entry. *(verified live: store empty after `_clear()`
      in run #2)*
- [x] A test exercises: write entry → simulate restart
      (re-instantiate `AgentManager`) → entry is reloaded.
      *(`test_agent_restart_recovers_session_without_creating_new_one`,
      `test_agent_seeded_from_persisted_store`)*
- [x] No new top-level dependency. *(stdlib `json`, `os`,
      `tempfile`, `threading`, `pathlib` only)*

### Verification

- 358 pytest passed (was 324 → +34 new tests in
  `tests/test_session_store.py`).
- `ruff check` + `ruff format --check`: clean (59 files).
- `mypy utils scufris_server`: no issues, 33 source files.
- Live smoke: `/tmp/nix-shell.cO2k21/opencode/smoke_p40.py` —
  three sequential server runs against live `opencode serve`
  on `:4096`, all four assertions passed; OpenCode side session
  count unchanged before/after (25 → 25, smoke cleaned up).

### Decisions

- **(b)** flat JSON with atomic rename, as recommended in the
  original task description. Tiny payload (a few bytes per user),
  no migration risk, easy to migrate to SQLite later if needed.
- Schema versioned (`{"version": 1, ...}`) so a future move to
  SQLite or a relational column has a clean upgrade path.
- Persistence is **opt-in** at the `AgentManager` constructor level
  (`session_store: Optional[SessionStore] = None`). Tests stay
  in-memory by default; only `bootstrap.py` wires up the real
  store. Keeps the test surface untouched and avoids forcing every
  caller into a particular path layout.
- Path order is `SCUFRIS_DATA_DIR > STATE_DIRECTORY > <repo>/data`.
  The systemd module already sets `StateDirectory=scufris` (mode
  0750) which makes `STATE_DIRECTORY=/var/lib/scufris` the
  production default with zero extra config. Dev runs from the
  repo write to `<repo>/data/opencode_sessions.json` (gitignore
  not adjusted — the `data/` dir doesn't exist until the server
  runs and the file path is benign to leak in a stray commit, but
  flagging for follow-up).
- `prune_invalid_sessions()` is best-effort: a `list_sessions()`
  failure logs a warning and returns 0 rather than wiping the
  map. A short OpenCode hiccup at startup must not erase users'
  conversational continuity.
- `replace_all()` is used by prune so the disk write happens once
  (not per dropped entry) — single fsync for the whole batch.

### Follow-ups (not in scope)

- Add `data/` to `.gitignore` if the repo-relative fallback ever
  ends up holding real data outside CI containers.
- Consider migrating to SQLite once we add anything beyond a
  scalar value per user (next obvious extension: per-session
  metadata for `/stats`).

---

## Original task description (preserved)

# Persist user_id - OpenCode session_id mapping across restarts

- STATUS: OPEN
- PRIORITY: 40
- TAGS: opencode,persistence

## Motivation

The OpenCode runtime swap (`tasks/20260610-101413`) keeps a per-user
session map (`AgentManager._sessions: Dict[int, str]`) in process
memory. When `scufris-server` restarts, that map is lost and the next
message from each user starts a fresh OpenCode session — *even though*
OpenCode itself still has the old sessions stored on disk (verified
during the spike: 20+ historic sessions for this project survived
across many restarts).

Result: history shown by `/v1/opencode/sessions` grows unboundedly,
users lose their conversational continuity, and `/v1/clear` no longer
deletes the right session ID after a restart.

The parent task lists this as Out. This task does it.

## Scope

### In

- Persist the `user_id → session_id` mapping somewhere that survives
  restart. Candidates:
  - **(a)** SQLite next to the existing `data/` history store.
  - **(b)** A flat JSON file (`data/opencode_sessions.json`) with
    atomic-rename writes.
  - **(c)** A column on whatever table `ChatHistoryManager` already
    persists.
  - Recommend **(b)** for v1 — fewest moving parts, matches the
    current "small JSON snapshot" idiom.
- Load the map at `AgentManager.__init__` from disk; write back on
  every mutation (cheap — small dict).
- On startup, validate persisted session IDs against
  `GET /session` — drop entries whose session no longer exists on the
  OpenCode side.
- `POST /v1/clear` removes the entry from disk too, not just from the
  in-memory dict.

### Out

- Sharing the map across multiple Scufris instances (HA). Single-host
  only.
- Encrypting the mapping (it's just opaque IDs).

## Acceptance criteria

- [ ] Restarting `scufris-server` does not start a new OpenCode session
      for users who had one before the restart.
- [ ] Stale entries (session deleted on the OpenCode side) are
      transparently pruned and the next message creates a fresh one.
- [ ] `POST /v1/clear` removes both the in-memory entry and the
      persisted entry.
- [ ] A test exercises: write entry → simulate restart (re-instantiate
      `AgentManager`) → entry is reloaded.
- [ ] No new top-level dependency.

## Open questions

- Where exactly to put the file (`data/`, `state/`, or alongside the
  existing history store).
- Schema versioning — if we eventually move to (a) or (c), we want a
  format that's easy to migrate.

## References

- `tasks/20260610-101413/TASK.md` — parent task; "Out" section flags
  this as future work.
- `utils/agent.py::AgentManager` — owner of the in-memory map post-swap.
- `utils/history.py::ChatHistoryManager` — existing persistence
  conventions to mirror.

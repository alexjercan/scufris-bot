# Identity layer + XDG user config (config.toml)

- STATUS: DONE
- PRIORITY: 75
- TAGS: identity,config

Carry over the v1 identity / config design (per-user TOML, surface
bindings for CLI/Telegram) onto the v2 `scufris-server` skeleton
landed by #9 (`tasks/20260613-091043`). Same shape as the v1 tasks
`20260520-145231` (original identity design) and `20260603-132426`
(TOML-first config refactor); the v1 implementation is gone (bot
removed during the v2 cut), so this task ports the *design*, not
code.

Implements:

- Design doc `tasks/20260613-091036/TASK.md` §11 (identity layer)
  and §9 endpoint `POST /v1/identity/resolve` (line 291).
- ADR-3 ("scufris owns identity; opencode is single-secret") — this
  task lands the scufris half.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).

---

## Scope

### In

A working identity layer for the v2 server:

1. A pure-Python module `scufris_server/identity.py` that:
   - Parses `~/.config/scufris/config.toml` into typed pydantic models.
   - Exposes `resolve_user(conn, surface, surface_id, identity_file,
     override_user_id)` which returns a `ResolvedUser` row,
     materialising the `surface_bindings` entry on first hit.
2. A FastAPI route `POST /v1/identity/resolve` (fills the
   placeholder at `scufris_server/routes/identity.py`).
3. A new server setting `SCUFRIS_USER_ID` (env-driven) that pins all
   resolves to a single user_id, bypassing TOML lookup. Useful for
   single-user dev/test deployments and the v1 "I am user N, period"
   carryover.
4. `routes/chat.py` switched from `DEFAULT_USER_ID = 1` to a real
   inline `resolve_user()` call, with surface_bindings materialising
   on first chat.
5. Lifespan (`scufris_server/app.py`) loads the TOML at startup and
   caches it on `app.state.user_identity`. Caches the override on
   `app.state.identity_override` (may be `None`).

### Explicitly out (delegated)

| Feature                                          | Owning task / note                                    |
|--------------------------------------------------|-------------------------------------------------------|
| Multi-user TOML (`[[users]]` array)              | Future — single-user matches v1 §11 carryover         |
| `[user.schedule]` parsing / scheduler            | Out of v2 scope (v1 feature; revisit if needed)       |
| `[user.rag]` / `[user.journal]` parsing          | Tool tasks (091038 journal, 091039 weather/web)       |
| `[server]` table parsing                         | Out — env vars (`config.py:Settings`) own deployment  |
| SIGHUP hot-reload of config.toml                 | Out (v1 explicitly deferred it too — restart needed)  |
| Hashed-username fallback when server unreachable | CLI concern, deferred to #14                          |
| Bearer-token auth on `/v1/identity/resolve`      | #14 (auth) — loopback-unauthenticated for v0          |

The placeholder `scufris_server/identity.py` docstring currently
mentions per-user TOML at `$XDG_CONFIG_HOME/scufris/users/<id>.toml`
— that's outdated. v1 + design §11 both use a single
`~/.config/scufris/config.toml`. Fix the docstring during step 3.

---

## TOML schema (v1 carryover)

Single-user. Schema mirrors what v1's `utils/user_config.py` accepted:

```toml
[user]
username = "alex"

[user.identity]
cli = "alex"
telegram = "8231376426"
# Forward-compatible: arbitrary surface keys allowed as long as values
# are strings (e.g. web = "alex@example.com" once a web surface lands).
```

Other v1 sub-tables (`[user.schedule]`, `[user.rag]`, `[user.journal]`,
`[user.notifications]`, `[server]`) are *ignored* by this task —
pydantic `extra="ignore"` — but the file remains parseable for the
future tasks that need them.

---

## Module layout

Touches:

```
scufris_server/
├── identity.py             # REPLACE placeholder — pure resolution module
├── config.py               # ADD: SCUFRIS_USER_ID, SCUFRIS_CONFIG settings
├── dependencies.py         # ADD: get_user_identity, get_identity_override
├── app.py                  # ADD: lifespan loads + caches user_identity
├── routes/
│   ├── __init__.py         # ADD: identity_router to ROUTERS
│   ├── identity.py         # FILL: POST /v1/identity/resolve
│   └── chat.py             # SWAP: DEFAULT_USER_ID=1 → resolve_user(...)
```

Tests touched:

```
tests/unit/
├── test_identity.py        # NEW — TOML parsing + resolve_user
├── test_identity_route.py  # NEW — POST /v1/identity/resolve
├── test_chat_route.py      # EXTEND — 3 identity scenarios
├── test_app.py             # EXTEND — lifespan loads user_identity
├── test_placeholders.py    # SHRINK — drop identity case (now real)
└── test_config.py          # EXTEND — SCUFRIS_USER_ID, SCUFRIS_CONFIG
```

---

## Design decisions (locked)

These are the four design questions answered before starting; record
them here so the task is self-contained.

### D1: TOML-miss fallback — bind to default user

When `POST /v1/identity/resolve {surface, surface_id}` is called and
the surface_id is *not* listed in `[user.identity]` for any TOML
user, insert a `surface_bindings` row pointing at `user_id = 1` (the
seeded "default" user). Matches v1 behaviour: any unrecognised
surface_id falls through to the default identity. Idempotent on
retry (PRIMARY KEY (surface, surface_id) collisions short-circuit
via SELECT-first).

Rejected alternatives: return 404 (would break the "no config, just
works" onboarding); auto-create new `users` row (v1 didn't do it;
introduces a write side-effect on a read endpoint).

### D2: Chat flow — inline resolve

`routes/chat.py` calls `resolve_user(conn, surface, surface_id,
identity_file, override_user_id)` before `_resolve_session`. One
indexed SELECT on `surface_bindings` per chat call (PRIMARY KEY
lookup, ~µs). Single source of truth; no caller-side trust issue.

Rejected alternatives: `ChatRequest.user_id` field (moots the
identity layer for the most-used endpoint); FastAPI middleware
(over-couples identity to all routes that don't need it, e.g.
`/v1/healthz`).

### D3: Pydantic model — lenient + typed

```python
class UserConfig(BaseModel):
    """The [user] table."""
    model_config = ConfigDict(extra="ignore")  # tolerate [user.schedule] etc.
    username: str  # required
    identity: dict[str, str] = Field(default_factory=dict)

class IdentityFile(BaseModel):
    """Root of config.toml."""
    model_config = ConfigDict(extra="ignore")  # tolerate [server], etc.
    user: UserConfig | None = None  # whole [user] table missing is OK
```

- `username` required when `[user]` is present (without it we can't
  uniquify into `users.username`).
- `identity` is a free `dict[str, str]` — any surface name allowed
  for forward compat (telegram, cli, web, slack, ...).
- Unknown top-level keys ignored, *not* errored — matches v1's "log
  a warning instead of failing" policy. (We don't actually warn in
  v2; pydantic silently drops them. Add a startup log line listing
  any ignored top-level keys for visibility.)

Rejected alternatives: strict (`extra="forbid"`) would break every
time a new surface or sub-table is added; plain dict skips
startup-time validation of malformed configs.

### D4: SCUFRIS_USER_ID — server-side override

`scufris_server.config.Settings.SCUFRIS_USER_ID: int | None = None`.
Read once at startup. If set, `app.state.identity_override = N`;
`resolve_user(override_user_id=N)` short-circuits — returns
`user_id=N` regardless of `(surface, surface_id)`, with *no*
surface_bindings insert (no side effects). The override's `username`
is looked up from the `users` table; if `user_id=N` doesn't exist
there, the server fails fast at startup (clearer than discovering it
on the first chat request).

This is a small deviation from v1 (which put the override on the
CLI side per design doc §11 line 427): v2 puts it on the server so
the future CLI (#14) doesn't have to special-case it, and so admins
can pin identity for dev/test without modifying TOML.

Rejected alternatives: drop it (loses the "just-pin-it" escape
hatch for dev); CLI-side only (forces every future surface to
re-implement the override).

---

## Additional micro-decisions

These came up while drafting the plan; recording for posterity.

- **TOML parse policy.** Syntax errors (`tomllib.TOMLDecodeError`)
  and schema violations (`ValidationError` from required `username`
  missing) raise at startup — the server refuses to boot. Missing
  file is OK (`IdentityFile(user=None)`). Unknown keys ignored.
- **ResolvedUser shape.** Per design §9 line 291: `{user_id,
  username, surface, surface_id, bound_surfaces}`. `bound_surfaces`
  is a `list[BoundSurface]` where `BoundSurface = {surface,
  surface_id}` — a snapshot of *all* current bindings for `user_id`
  (lets clients show "you're known as X on telegram, Y on cli").
- **Idempotency.** `resolve_user` does SELECT first, INSERT only if
  the (surface, surface_id) pair is absent. Avoids PRIMARY KEY
  collisions on retry; double-call returns the same row.
- **Override + TOML interaction.** When override is set, TOML is
  *not* consulted and *no* surface_bindings rows are written. The
  override is a read-only short-circuit. The `bound_surfaces` field
  in the response still reflects the actual DB state for `user_id=N`.
- **`SCUFRIS_CONFIG` env var.** Mirror v1's lookup order: env var
  `SCUFRIS_CONFIG` wins, else `$XDG_CONFIG_HOME/scufris/config.toml`,
  else `~/.config/scufris/config.toml`. Default user (id=1, "default")
  is seeded by the existing lifespan migration step (#9) regardless
  of whether the TOML lists a "default" user.

---

## TODOs (in implementation order)

Each step is small enough to land in one commit and review independently.

1. **Update `tasks/20260613-091046/TASK.md`.** [this file]
   - [x] Expand from 13-line stub to full scope (this rewrite).

2. **`config.py` settings.**
   - [x] Add `SCUFRIS_USER_ID: int | None = None` to `Settings`.
   - [x] Add `SCUFRIS_CONFIG: Path | None = None` to `Settings`
         (explicit TOML path; defaults to XDG lookup when `None`).
   - [x] Extend `tests/unit/test_config.py` with env-var override
         tests for both.

3. **`identity.py` module.**
   - [x] Replace placeholder docstring (no per-user `<id>.toml`).
   - [x] Define `BoundSurface`, `UserConfig`, `IdentityFile`,
         `ResolvedUser` pydantic models.
   - [x] `load_user_identity(path: Path | None) -> IdentityFile`:
         resolves XDG path if `None`, returns empty file when missing,
         raises on TOML/Validation errors. Logs ignored top-level
         keys at INFO.
   - [x] `resolve_user(conn, surface, surface_id, identity_file,
         override_user_id=None) -> ResolvedUser`: pure function that
         handles override / TOML-hit / fallback paths; SELECT-then-
         INSERT for idempotency; queries `users.username` for the
         resolved id.
   - [x] No FastAPI imports — keep the module driver-agnostic so the
         future scheduler (background task) can call it too.

4. **Unit tests for `identity.py`.**
   - [x] `test_load_user_identity_*`: valid file, missing file
         (returns empty), TOML syntax error (raises), schema
         violation (raises), extra keys ignored.
   - [x] `test_resolve_user_*`: TOML hit (alex), TOML miss → default
         binding inserted, idempotent (second call returns same row,
         no second insert), override wins regardless of binding.
   - [x] `test_resolve_user_override_missing_user`: raises when
         `override_user_id` points at non-existent users row.

5. **`routes/identity.py` route.**
   - [x] Fill the placeholder router: `POST /v1/identity/resolve`
         accepts `{surface, surface_id}`, returns `ResolvedUser`.
   - [x] Depends on `get_db_conn`, `get_user_identity`,
         `get_identity_override` (new dependencies in `dependencies.py`).
   - [x] Validation: surface + surface_id both `min_length=1`.

6. **Unit tests for the route.**
   - [x] `test_identity_route.py`: happy path (alex), fallback
         (unknown → default), override (returns pinned id, ignores
         surface).
   - [x] Reuse the in-memory DB fixture pattern from
         `test_chat_route.py`.

7. **Lifespan wiring (`app.py` + `routes/__init__.py`).**
   - [x] In lifespan, after `apply_migrations`:
         `app.state.user_identity = load_user_identity(settings.SCUFRIS_CONFIG)`.
   - [x] Validate override: if `settings.SCUFRIS_USER_ID is not None`,
         `SELECT 1 FROM users WHERE id = ?` — raise at startup if
         absent. Set `app.state.identity_override`.
   - [x] Add `identity_router` to `ROUTERS` in `routes/__init__.py`.
   - [x] Add `get_user_identity` + `get_identity_override` to
         `dependencies.py`.

8. **Lifespan + placeholder tests.**
   - [x] `tests/unit/test_app.py`: assert `app.state.user_identity`
         is an `IdentityFile`; assert override validation raises on
         missing user_id; assert identity_router is mounted.
         (Mount check landed in `test_identity_route.py::test_identity_route_is_mounted`
         and `test_placeholders.py::test_routes_init_imports_placeholders`
         (`len(ROUTERS) == 3`); no explicit duplicate in `test_app.py`.)
   - [x] `tests/unit/test_placeholders.py`: drop identity case (now
         real); keep sessions/stats/permissions cases.
   - [x] Add a `tmp_xdg_config` fixture that writes a small TOML
         under `tmp_path` and points `XDG_CONFIG_HOME` at it.
         (Inlined as ``monkeypatch.setenv("XDG_CONFIG_HOME", ...)`` +
         `_write_config_toml` in the single XDG-fallback test, since
         the fixture would have had exactly one caller. Helper
         function is shared across `test_app.py` /
         `test_identity_route.py` / `test_chat_route.py`.)

9. **`routes/chat.py` integration.**
   - [x] Remove `DEFAULT_USER_ID = 1`.
   - [x] Call `resolve_user(conn, channel.surface, channel.surface_id,
         app.state.user_identity, app.state.identity_override)`
         before `_resolve_session`.
   - [x] Pass `resolved.user_id` into the existing channel lookup /
         insert paths.

10. **Extend `tests/unit/test_chat_route.py`.**
    - [x] Three new scenarios:
          - TOML hit: surface_id="alex" with TOML alex → user 2.
          - Miss: unknown surface_id → user 1 (default), binding inserted.
          - Override: `app.state.identity_override = 1` → user 1 regardless.
    - [x] Verify surface_bindings row created exactly once across
          two back-to-back calls with the same channel.

11. **README updates.**
    - [x] Add "User identity" section: config.toml location, full
          schema example, `SCUFRIS_USER_ID` + `SCUFRIS_CONFIG` env
          var notes, `curl POST /v1/identity/resolve` example.
    - [x] Cross-reference from the existing "Running scufris-server"
          section's env-vars table.

12. **Live verification.**
    - [x] Write `~/.config/scufris/config.toml` with `[user]
          username = "alex"` + `[user.identity] cli = "alex"`.
          (Used `/tmp/scufris-step12/config.toml` via `SCUFRIS_CONFIG`
          to avoid mutating the real user config.)
    - [x] Restart `scufris-server`.
    - [x] `curl POST /v1/identity/resolve` for `(cli, alex)` → user
          2 (or whatever the new row's id is); for `(cli, stranger)`
          → user 1.
    - [x] `SCUFRIS_USER_ID=1 python -m scufris_server` → both
          resolves return user 1.
    - [x] `POST /v1/chat` with `channel.surface_id="alex"` → reply
          attributed to alex's user_id; verify in sqlite that
          surface_bindings has one new row.

13. **Cleanup + sign-off.**
    - [x] `uv run --active pytest` green (unit only — integration
          test from #9 still passes too).
    - [x] `uv run --active ruff check`, `uv run --active mypy --strict
          scufris_server` clean.
    - [x] Mark STATUS: DONE here with closing notes (live-verified
          IDs/timings, follow-up tasks filed if any).

---

## Acceptance criteria

- [x] `POST /v1/identity/resolve {surface: "cli", surface_id: "alex"}`
      returns `{user_id, username: "alex", surface, surface_id,
      bound_surfaces: [...]}` when TOML lists alex; creates row in
      `users` + `surface_bindings` on first call; idempotent on
      retry.
- [x] Same endpoint with unrecognised surface_id returns the default
      user (id=1, "default"), with a new `surface_bindings` row.
- [x] `SCUFRIS_USER_ID=N` pins all resolves to user N, with no
      surface_bindings side-effects; server fails to boot if user N
      doesn't exist in the `users` table.
- [x] `POST /v1/chat` no longer hard-codes user_id=1: chat history
      lands under the resolved user.
- [x] `~/.config/scufris/config.toml` parses without errors for the
      v1 example file (i.e. existing v1 users can drop their TOML in
      and v2 reads it). Unknown sub-tables (`[user.schedule]` etc.)
      are silently ignored.
- [x] Missing config.toml is OK — server boots, all resolves go to
      default user.
- [x] Malformed TOML or missing `[user].username` raises at startup.
- [x] All existing unit tests still pass (143 → 143+); new tests
      cover identity loading, resolve, route, and chat integration.
      (Final count: 189 unit tests; +46 from the #12 baseline of 143.)
- [x] `ruff check` + `mypy --strict scufris_server` clean.

---

## Dependencies & sequencing

- **Hard-blocks**: #9 (`20260613-091043`, DONE) — provides the
  server skeleton, `users`/`surface_bindings` schema, and the
  `routes/identity.py` + `identity.py` placeholders.
- **Soft-depends-on**: nothing else. Design doc (091036) is CLOSED.
- **Unblocks**: this task is on Path A (identity → sessions →
  CLI). Once done:
  - #10 (`20260613-091044`, sessions) can consume `resolve_user()`
    to scope `/v1/sessions` listings per user.
  - #14 (`20260613-091049`, CLI) can call `/v1/identity/resolve`
    once at startup with `(cli, $USER)`.
  - #11 (`20260613-091045`, SSE) inherits the same identity model
    once it picks up `chat.py`.

---

## Open questions

Small implementation-time decisions; none block starting.

1. **`bound_surfaces` ordering.** Sort by `surface` ASC, then
   `surface_id` ASC? Or insertion order? Picking deterministic
   (alphabetical) for test stability — confirm during step 3.
   **Resolved:** alphabetical. `_bound_surfaces` SELECT uses
   `ORDER BY surface ASC, surface_id ASC`. Test stability + stable
   client rendering trumped insertion-order locality (which would
   leak DB write timing into the response).
2. **Override startup-failure error type.** Reuse the v1
   `ConfigError`-style exception, or just `RuntimeError`? Lean
   `RuntimeError` — keeps the module import surface small. Confirm
   during step 7.
   **Resolved:** plain `RuntimeError` with a structured message
   ("SCUFRIS_USER_ID=N is set but no user with that id exists.
   Either remove the env var, lower it to 1 (default user), or
   pre-seed the users table via config.toml."). No custom
   exception class needed; lifespan failure already kills the
   process via uvicorn's "Application startup failed" path.
3. **Logging the resolve outcome.** Log at INFO on first-bind
   (interesting, infrequent) and at DEBUG on cache-hit (frequent,
   noisy)? Yes — matches existing chat.py logging style. Confirm
   during step 3.
   **Resolved:** as proposed. INFO on TOML hit + default fallback
   (both write a binding); DEBUG on cache hit + override (both
   read-only). Each line carries `extra={user_id, username,
   surface, surface_id, source}` for `JsonFormatter` lift.

---

## Closing notes (Step 13)

Landed on 2026-06-14. Path A unblocked: #10 (sessions) and #14 (CLI
v2) can now consume `resolve_user()` directly.

### Final test counts

| Bucket                               | Count | Δ vs #12 baseline |
|--------------------------------------|-------|-------------------|
| `tests/unit/`                        | 189   | +46               |
| `tests/integration/` (opt-in)        | 1     | unchanged (#9)    |
| `ruff check scufris_server tests`    | clean | —                 |
| `mypy --strict` (32 source files)    | clean | —                 |

New unit-test files / extensions:

- `tests/unit/test_identity.py` (NEW) — 26 tests covering xdg path
  resolution, TOML happy/error/leniency paths, and all four
  `resolve_user` paths + idempotency + bound_surfaces aggregation.
- `tests/unit/test_identity_route.py` (NEW) — 9 tests covering all
  four resolution paths through the HTTP layer + 422 validation
  surface + mount smoke check.
- `tests/unit/test_app.py` (EXTEND) — +6 tests for lifespan-loaded
  identity + override validation (incl. RuntimeError-on-stale-id).
- `tests/unit/test_chat_route.py` (EXTEND) — +3 tests for chat
  flowing through identity resolution (TOML hit, override, default
  fallback + cache).
- `tests/unit/test_config.py` (EXTEND) — +5 tests for
  `SCUFRIS_USER_ID` validation + `SCUFRIS_CONFIG` plumbing.
- `tests/unit/test_placeholders.py` (SHRINK) — dropped 2
  `identity` cases (module + route now real); ROUTERS count
  bumped 2 → 3.

### Live verification (Step 12)

Sandbox: `/tmp/scufris-step12/` (config + state); server on
`127.0.0.1:7081`; real `opencode serve` 1.15.13 + `ollama`
`qwen3:latest`. Did NOT touch `~/.config/scufris/` or
`$XDG_STATE_HOME/scufris/`. Sandbox removed at end.

| Scenario                                            | Verified |
|-----------------------------------------------------|----------|
| Default fallback `(cli, unknown)` → user_id=1       | yes      |
| TOML hit `(cli, alex)` → user_id=2 (new alex row)   | yes      |
| TOML hit `(telegram, 8231376426)` → user_id=2 too   | yes      |
| `bound_surfaces` aggregates both, sorted            | yes      |
| Live `/v1/chat` from `(cli, alex)` → channels.user_id=2 | yes  |
| Override `SCUFRIS_USER_ID=1` pins TOML hit to user 1, no binding writes | yes |
| Override `bound_surfaces` scoped to override user only | yes   |
| Stale override `SCUFRIS_USER_ID=999` → boot RuntimeError | yes |
| Idempotency: repeat `(cli, unknown)` → 1 binding row | yes     |

Boot logs confirmed correct paths:
- No TOML → `"no config.toml at ...; default user only"`
- TOML present → `"loaded config.toml: user=alex"`
- Override → `"identity override active: user_id=1 (default)"`
- Stale → `"Application startup failed. Exiting."` with structured
  `RuntimeError` traceback.

Live `/v1/chat` round-trip latency: ~30 s cold (qwen3 first inference),
returned the expected `"pong"` reply with token counts (input=4096,
output=182) and `cost=0.0` (ollama is free).

### Deviations from plan

1. **Step 8 `tmp_xdg_config` fixture.** Originally specced as a
   reusable pytest fixture; ended up inlined as
   `monkeypatch.setenv("XDG_CONFIG_HOME", ...)` + `_write_config_toml`
   helper in the single XDG-fallback test, since the fixture would
   have had exactly one caller. The `_write_config_toml` helper *is*
   shared across `test_app.py`, `test_identity_route.py`, and
   `test_chat_route.py` — that's the actual deduplication win.
2. **Step reorder (6 vs 7).** Original order was 6 (route tests)
   then 7 (lifespan). Flipped because `TestClient` triggers lifespan
   which must populate `app.state.user_identity` /
   `identity_override` first. Final landing order: 1→2→3→4→5→7→8→6→9→10→11→12→13.
3. **`identity_router` mount assertion.** TODO 8 asked for an
   explicit assertion in `test_app.py`; the equivalent check landed
   in `test_identity_route.py::test_identity_route_is_mounted` and
   transitively in `test_placeholders.py::test_routes_init_imports_placeholders`
   (`len(ROUTERS) == 3`). Coverage is fine; no duplicate added.

### Follow-ups filed

None. Live verification matched the locked design decisions
exactly; no scope creep, no deferred edges. Path A continues with
#10 (sessions) next.

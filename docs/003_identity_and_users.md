# 003 — Identity and users

This doc covers everything about *who is making this request* —
how scufris-server maps an incoming `(surface, surface_id)` pair to a
`users.id`, how `config.toml` participates, what the override does,
and what the edge cases look like.

Source: `scufris_server/identity.py`. The whole identity layer is one
self-contained module — no FastAPI dependency, ~470 lines of
docstrings + code. Pure-Python so future scheduler / CLI tools can
call `resolve_user()` directly.

## Mental model

Scufris is single-user *today*, but multi-user *eventually*. The
schema reflects that — there's a `users` table with multiple rows, a
`surface_bindings` table mapping `(surface, surface_id) → user_id`,
and the resolver materialises rows lazily as users show up on new
surfaces.

The vocabulary:

- **Surface** — a user-facing channel. Today: `"cli"`, `"telegram"`.
  Tomorrow: `"web"`. Free-form strings, no enum constraint, so
  adding a new surface doesn't require a code change in the
  identity layer.
- **surface_id** — whatever uniquely names the user *on that surface*:
  - `"cli"` → terminal username (`"alex"`).
  - `"telegram"` → chat_id (`"8231376426"`).
  - `"web"` → tab id, session cookie, or whatever the future web
    client picks.
- **user_id** — the canonical primary key. `users.id`. This is what
  `channels` rows are keyed by, and what every `facts` row will be
  scoped to.

Why decouple `surface_id` from `user_id`? Because the same human can
appear on multiple surfaces with different surface_ids — `alex` on
the CLI, chat_id `8231376426` on Telegram — and we want both calls to
land in the same row of `facts`, the same per-user history, etc.

## The default user

A row `users(id=1, username="default", created_at=<now>)` is seeded
into the table on every boot (lifespan step 2; `app.py:95`). The
insert is `INSERT OR IGNORE`, so it's a no-op after the first run.

This row is the "fallback bucket" for any incoming `(surface,
surface_id)` that doesn't match `config.toml`. Without it, the very
first request from any non-listed surface_id would fail because there
would be no user to bind to.

You can disable the fallback effectively by setting `SCUFRIS_USER_ID=N`
to pin every request to a specific known-good user — see "Override"
below.

## TOML: where users come from

`config.toml` is the source of truth for "real" (non-default) users.
The shape is single-user and v1-compatible:

```toml
[user]
username = "alex"

[user.identity]
cli      = "alex"
telegram = "8231376426"
```

`[user]`:

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `username` | `string` | yes | Becomes `users.username`. Must be unique across the table. |
| `identity` | `dict[str, str]` | no | Surface name → surface_id. Free-form keys. Default `{}`. |

`[user.identity]`:

Each key is a surface name; the value is the surface_id for *this
user on that surface*. Used by step 3 of the resolver: when an
incoming request matches `(surface, surface_id) = (key, value)`, the
resolver binds the request to this user.

### What's tolerated and what's not

The pydantic models use `extra="ignore"` at every level:

- Top-level keys other than `[user]` (like `[server]`, `[scheduler]`,
  `[user.schedule]`, `[user.rag]`, `[user.journal]`,
  `[user.notifications]`) — ignored. Logged at INFO at load time so
  operators can spot stale config.
- Extra keys inside `[user]` — ignored.
- Extra keys inside `[user.identity]` — ignored (well, every key is
  treated as a surface name, so "extra" doesn't really apply here).

Hard errors (the lifespan refuses to start):

- Malformed TOML syntax → `tomllib.TOMLDecodeError` propagates.
- `[user]` present but `username` missing → `pydantic.ValidationError`.
- `[user.identity]` value isn't a string → `pydantic.ValidationError`
  (the type is `dict[str, str]`).

We deliberately fail loud on schema violations rather than silently
downgrade everyone to the default user — a typo'd config left
running for a week is much harder to debug than a startup crash.

### TOML location resolution

`load_user_identity(path)` (`identity.py:152`) resolves the TOML
location like this:

1. **Explicit argument wins.** Tests pass `path=tmp_path / "config.toml"`
   directly. Callers from inside the lifespan pass
   `Settings.config_path` (which is `SCUFRIS_CONFIG` if set, else
   `None`).
2. **`SCUFRIS_CONFIG` env var.** If set, `Settings.config_path`
   carries it through to `load_user_identity`. Used for non-XDG
   locations, e.g. `/etc/scufris/config.toml` for system-wide
   deploys.
3. **`$XDG_CONFIG_HOME/scufris/config.toml`.** When `XDG_CONFIG_HOME`
   is set in the environment.
4. **`~/.config/scufris/config.toml`.** Final fallback.

A non-existent file is *not* an error: `load_user_identity` returns
an empty `IdentityFile(user=None)` and logs `"no config.toml at
<path>; default user only"` at INFO. Every request will then fall
through to the default user.

## The four-step resolver

`resolve_user(conn, surface, surface_id, identity_file, override_user_id)`
(`identity.py:306`) consults sources in this order:

```
                       ┌──────────────────────────┐
                       │ Step 1: SCUFRIS_USER_ID  │
                       │       override?          │
                       └──────────────┬───────────┘
                                      │
                            yes ──────┴────── no
                            │                  │
                            ▼                  ▼
                  ┌───────────────────┐  ┌──────────────────────────┐
                  │ return that user  │  │ Step 2: surface_bindings │
                  │ as-is. No writes. │  │   row already exists?    │
                  └───────────────────┘  └──────────────┬───────────┘
                                                        │
                                              yes ──────┴────── no
                                              │                  │
                                              ▼                  ▼
                                    ┌──────────────────┐  ┌────────────────────────────┐
                                    │ return cached    │  │ Step 3: TOML matches       │
                                    │ user. No writes. │  │ (user.identity[surface]    │
                                    └──────────────────┘  │       == surface_id)?      │
                                                          └──────────────┬─────────────┘
                                                                         │
                                                              yes ──────┴────── no
                                                              │                  │
                                                              ▼                  ▼
                                            ┌──────────────────────────┐ ┌────────────────────┐
                                            │ insert users row if new, │ │ Step 4: bind to    │
                                            │ insert surface_bindings, │ │ default user (id=1)│
                                            │ return TOML user.        │ │ insert binding,    │
                                            └──────────────────────────┘ │ return.            │
                                                                         └────────────────────┘
```

Every path returns a `ResolvedUser`. The only exceptions are
mis-seeded DB states — see "Edge cases" below.

### Step 1 — Override (`SCUFRIS_USER_ID`)

Source: `identity.py:347-375`.

If `override_user_id is not None`, the resolver:

- Looks up `users.username` for that id.
- If found, returns `ResolvedUser(user_id=override, username=..., ...)`
  with `bound_surfaces` pulled from `surface_bindings` for that user.
- If *not* found, raises `RuntimeError`. (In practice this never
  happens in production: the lifespan validates the override against
  the table at boot — see step 4 of the lifespan.)

**No writes.** The override path skips `surface_bindings` insert
entirely. This is deliberate: when you remove the override later, the
table should still be clean.

The override is intended for single-user dev/test deploys where you
already know the right user_id and don't want every random surface
to materialise a binding. See `004_configuration.md` for the env-var
mechanics.

### Step 2 — Existing binding

Source: `identity.py:377-411`.

`SELECT user_id FROM surface_bindings WHERE surface=? AND surface_id=?`.

- Hit → return that user. No writes. Fast path; this is what every
  call after the first one looks like.
- Miss → fall through to step 3.

Bindings are *sticky*. Once `(surface, surface_id) → user_id` is
written, it stays that way regardless of TOML edits. To re-route an
existing binding, the operator has to manually `DELETE FROM
surface_bindings WHERE surface=? AND surface_id=?` and let the next
call re-resolve. That's intentional: silent re-routing of long-lived
channels would be very confusing to debug.

### Step 3 — TOML hit

Source: `identity.py:413-438`.

If `identity_file.user is not None` and
`identity_file.user.identity.get(surface) == surface_id`, the user
is "us" — the TOML user.

- `_ensure_user_row(conn, username)`: SELECT / INSERT pattern. Returns
  `users.id` for the username. Lazily inserts on first hit.
- `_ensure_surface_binding(conn, user_id, surface, surface_id)`:
  inserts the binding row. Idempotent.
- Logs at INFO: `"TOML resolve: user_id=2 (alex) for (cli, alex)"`.
- Returns the resolved user.

This is a one-time-per-surface event for any given user. Subsequent
calls cache-hit at step 2.

### Step 4 — Default fallback

Source: `identity.py:440-467`.

Reached when:
- No override.
- No existing binding.
- No TOML match (either no `[user]` at all, or the surface_id doesn't
  match `[user.identity]`).

Action:
- Look up `users.username` for `DEFAULT_USER_ID` (= 1). If missing →
  `RuntimeError` (lifespan seed broke).
- Insert a `surface_bindings` row pointing this `(surface,
  surface_id)` at the default user.
- Logs at INFO: `"default resolve: user_id=1 (default) for
  (cli, stranger)"`.
- Returns the resolved user.

So *every* surface_id that the resolver has seen ends up with a
binding row — either to a TOML-defined user or to the default user.
This means future calls are always step-2 cache hits.

## Worked examples

Setup: `config.toml` with `[user]` `username="alex"`,
`[user.identity]` `cli="alex"`, `telegram="8231376426"`. No override.

### Example 1: First CLI call from alex

Request: `(cli, alex)`.

- Step 1: no override, skip.
- Step 2: no `surface_bindings` row, miss.
- Step 3: `identity.get("cli") == "alex"` → match. Insert `users(2,
  "alex", ...)`, insert `surface_bindings(2, "cli", "alex")`. Return
  user 2.
- Bound surfaces returned: `[("cli", "alex")]`.

### Example 2: First Telegram call from alex's phone

Request: `(telegram, "8231376426")`.

- Step 1: skip.
- Step 2: miss.
- Step 3: `identity.get("telegram") == "8231376426"` → match. User
  row already exists (from example 1). Insert `surface_bindings(2,
  "telegram", "8231376426")`. Return user 2.
- Bound surfaces returned: `[("cli", "alex"), ("telegram",
  "8231376426")]` — sorted ASC.

So now both surfaces are bound to user 2, and the same `(user_id=2,
agent)` opencode session can serve both. Per-user `facts` written
from CLI are visible from Telegram.

### Example 3: Stranger from CLI

Request: `(cli, "bob")`.

- Step 1: skip.
- Step 2: miss.
- Step 3: `identity.get("cli") == "alex"`, but `surface_id == "bob"`
  → mismatch. Skip.
- Step 4: bind to default user. Insert `surface_bindings(1, "cli",
  "bob")`. Return user 1 ("default").
- Bound surfaces: every binding for user 1 — could be a long list as
  unknown surface_ids accumulate.

bob will share state with everyone else routed to the default user.
That's fine for a single-user deployment where "everyone unknown =
me anyway"; it's a deliberate non-feature for actual multi-user
serving (which is a future task).

### Example 4: Same call, second time

Request: `(cli, "alex")`, after example 1.

- Step 1: skip.
- Step 2: hit. Return user 2 directly.
- No writes. No INFO log; one DEBUG log line.

Steady state.

## Override semantics

Set `SCUFRIS_USER_ID=N` and:

- Every `resolve_user` call returns `user_id=N` regardless of
  `surface`/`surface_id`/TOML/cache.
- No `users` writes.
- No `surface_bindings` writes.
- The `bound_surfaces` field in the response still reflects whatever
  bindings exist for user N (because we read them from SQLite).
- The lifespan validates `N` against `users` at boot (`app.py:111`).
  Stale id → process refuses to start with:

  ```
  RuntimeError: SCUFRIS_USER_ID=999 is set but no user with that id
  exists. Either remove the env var, lower it to 1 (default user),
  or pre-seed the users table via config.toml.
  ```

  This is preferable to discovering it on the first chat request.

When to use the override:

- Single-user dev/test deploys where you don't care about the TOML.
- Pinning the v2 carryover to whatever id the v1 deployment used,
  if you're migrating data.
- Smoke tests / fixtures.

When *not* to use the override:

- Any deployment where you actually want different surfaces to map
  to different users (i.e. anything multi-user).

## Edge cases and failure modes

| Situation | What happens |
|-----------|--------------|
| `config.toml` missing | Empty `IdentityFile`. All requests fall through to default user. Logged INFO `"no config.toml at <path>; default user only"`. |
| `config.toml` malformed (syntax) | `tomllib.TOMLDecodeError` propagates → lifespan fails → process exits. By design. |
| `[user]` present, `username` missing | `pydantic.ValidationError` propagates → lifespan fails. By design. |
| `SCUFRIS_USER_ID=999` and no row 999 | `RuntimeError` → lifespan fails → process exits. By design. |
| Default user row deleted manually | Step 4 raises `RuntimeError` on the first request that reaches it. Operator-visible 500. |
| `surface_bindings` row points at deleted user | Step 2 raises `RuntimeError` on the next request matching that binding. Operator-visible 500. |
| Two different TOML users claiming the same surface_id | Whoever resolves first wins; the binding is sticky. Subsequent attempts fall through to step 4 (no TOML match for the *taken* binding) — but step 2 short-circuits anyway. To unstick, delete the binding row by hand. |
| TOML user added later, after the surface already bound to default | Step 2 still hits → returns default user. To re-route, delete the binding row, then the next call hits step 3. |

## Logging the resolver

| Event | Level | When |
|-------|-------|------|
| `loaded config.toml: user=alex` | INFO | Lifespan, on TOML found. |
| `no config.toml at <path>; default user only` | INFO | Lifespan, on TOML missing. |
| `ignoring top-level keys in <path>: [server, scheduler, ...]` | INFO | Lifespan, on TOML loaded with extras. |
| `identity override active: user_id=2 (alex)` | INFO | Lifespan, on `SCUFRIS_USER_ID` set + validated. |
| `TOML resolve: user_id=2 (alex) for (cli, alex)` | INFO | First-time bind via TOML. Once per user/surface. |
| `default resolve: user_id=1 (default) for (cli, stranger)` | INFO | First-time bind via fallback. Once per surface_id. |
| `cached resolve: ...` | DEBUG | Step 2 cache hit. Frequent. |
| `override resolve: ...` | DEBUG | Step 1. Frequent. |
| `identity resolve: user_id=N (...) for (...)` | INFO | At the route handler in `routes/identity.py` after resolution. Has `request_id` attached. |

INFO-only at first-bind keeps the log volume bounded: in a steady
multi-day deployment with stable surface_ids, you should see no
identity log lines after the first hour or so.

## Cross-references

- TOML location and env vars: [`004_configuration.md`](004_configuration.md).
- Schema for `users` and `surface_bindings`: [`005_data_model.md`](005_data_model.md).
- The chat handler that calls `resolve_user()`: `routes/chat.py:224`.
- The dedicated identity route: `routes/identity.py:61`.
- v1's original identity design (preserved verbatim in v2):
  `tasks/20260520-145231/TASK.md`.

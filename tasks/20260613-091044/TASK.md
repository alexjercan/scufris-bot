# Per-user opencode session management (list + clear)

- STATUS: CLOSED
- PRIORITY: 85
- TAGS: server,sessions,memory

Land the user-facing **session management surface** from the v2
design doc: list a user's channels (enriched with opencode metadata),
clear a single channel's session link, clear all of a user's session
links. Chat-time session create/reuse already shipped as part of #9
step 8 (`routes/chat.py:_resolve_session/_record_new_session/_touch_session`);
this task adds the *management* endpoints around it and extracts the
shared bits into a service module.

Implements:

- Design doc `tasks/20260613-091036/TASK.md` §8 lines 294, 296, 297
  (GET sessions, POST clear-one, POST clear-all).
- The "create / reuse / cleanup" half of the original #10 brief
  (create/reuse already in chat.py; cleanup = clear endpoints).

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).

Two follow-ups filed during this task's scoping (2026-06-16):

- **#33 (`20260616-111428`)** — Fork endpoint
  (`POST /v1/sessions/:channel_id/fork`). Design §8 line 295, §9.4,
  ADR-13. Split out because fork carries non-trivial channel-identity
  design weight (D1 captured in #33's body) and the task title for
  #10 only names create/resume/expire.
- **#34 (`20260616-111430`)** — Auto-expiry of stale `session_links`
  by TTL on `last_used_at`. Mentioned in #10's body ("...cleanup /
  expiry") but no design existed; split out as a pure-housekeeping
  follow-up that doesn't gate this task's user-facing surface.

---

## Scope

### In

1. **Service module** `scufris_server/sessions.py` (NEW): the data-
   plane operations behind both chat and the new HTTP routes:
   - `create_channel_link(conn, user_id, surface, surface_id, agent,
     oc_session_id) -> int` — shared with chat.py; replaces the
     private `_record_new_session` there.
   - `list_user_channels(conn, user_id) -> list[ChannelRow]` —
     joins `channels` × `session_links` for the GET endpoint.
   - `get_channel(conn, channel_id) -> ChannelRow | None` —
     single-row lookup for the per-channel clear endpoint.
   - `clear_channel_link(conn, channel_id) -> bool` — delete the
     `session_links` row; True if a row was removed, False if
     none existed. Channel row is preserved.
   - `clear_user_links(conn, user_id) -> int` — bulk delete; returns
     deleted count.
2. **OpencodeClient extensions** (`scufris_server/opencode_client.py`):
   - Extend `Session` model with typed `title`, `tokens`, `cost`,
     `time` fields (currently only `id` is typed; rest live in
     `model_extra`). Add a `SessionTime` sub-model for `time.{created,
     updated}`.
   - `list_sessions() -> list[Session]` wrapping `GET /session`.
3. **HTTP routes** in `scufris_server/routes/sessions.py`:
   - `GET /v1/sessions?user_id=N` → list of channels enriched with
     opencode metadata. Degrades to nulls if opencode unreachable
     (D2); empty array for a valid user with no channels; 404 for
     unknown user_id.
   - `POST /v1/sessions/:channel_id/clear {}` →
     `{cleared: true|false}`. 404 if channel unknown or not owned
     by the resolved user.
   - `POST /v1/clear {user_id}` → `{count: N}`. Returns 0 for users
     with no links rather than 404 (idempotent bulk op).
4. **Chat refactor**: `_record_new_session` delegates to
   `sessions.create_channel_link`. `_resolve_session` and
   `_touch_session` stay private to chat.py (chat-loop specific,
   not reused).
5. **Test coverage**: ~12 route tests + ~8 service tests + 1
   integration test against live opencode.

### Out

| Feature                                    | Owning task / note                          |
|--------------------------------------------|---------------------------------------------|
| Fork (`POST /v1/sessions/:channel_id/fork`)| #33 (`20260616-111428`)                     |
| Auto-expiry of stale `session_links`       | #34 (`20260616-111430`)                     |
| Get-one (`GET /v1/sessions/:channel_id`)   | Not in design §8; defer until a caller wants|
|                                            | it. List endpoint already returns the row.  |
| Pagination on `list_sessions()`            | Single-user single-project v0 — small N. We |
|                                            | currently see ~50 sessions on the live host;|
|                                            | acceptable. Revisit if N grows.             |
| Per-user authz beyond override + existence | Single-host trust model (ADR-8, ADR-11).    |
|                                            | Bearer-token auth deferred to #14.          |
| `GET /v1/sessions/:channel_id/messages`    | Out — opencode owns messages; surface       |
|                                            | proxying lives in #13 (stats) or later.     |

---

## Design decisions (locked during scoping, 2026-06-16)

These are answered up front so the implementation steps don't
re-litigate them. Question/options matrix in the chat scrollback
(D0/D1/D2 — D0 = split fork, D1 = N/A here, D2 = degrade).

### D1: GET /v1/sessions degrades when opencode is unreachable

When the GET endpoint cannot reach opencode for the `list_sessions()`
enrichment call, return **200 with `title=null, tokens=null,
cost=null`** for every row and log a single WARNING. The DB-side
data (channel_id, channel descriptor, oc_session_id, last_used_at)
is always present.

Rationale: this is a read endpoint that the CLI/Telegram surfaces
will hit to render "your sessions" lists. If opencode flaps, the
user can still see what they have. Contrast with `/v1/chat` which
*requires* opencode to function (503 is correct there).

Rejected alternative: 503 like chat. Consistent error policy but
worse UX during transient opencode outages.

### D2: Authz model — override dominates, else validate exists

For all three endpoints, user_id comes from the request (query
string for GET, body for POST `/v1/clear`, or implicit via
`channel_id`-owning-user for POST `/v1/sessions/:channel_id/clear`).
Resolution rules:

1. **If `app.state.identity_override` is set**: ignore the
   client-supplied user_id, use the override. No 404 on mismatch —
   the override is a deliberate detour that always wins. Mirrors
   #12 D4 (override semantics on chat).
2. **Else**: validate the supplied user_id exists in `users`. If
   not, return 404 (with the structured `{error, error_type}` body
   shape established in chat). No per-user auth — single-host
   trust model.

For `POST /v1/sessions/:channel_id/clear`: look up the channel,
check `channels.user_id == resolved_user_id`. 404 if mismatch or
unknown channel. The implicit-via-channel lookup means the
override case stays consistent: override-pinned user can only
clear their own channels.

### D3: Service module — flat, not under `services/`

Single `scufris_server/sessions.py` module (mirrors
`scufris_server/identity.py` from #12) rather than introducing a
`scufris_server/services/` namespace. Repo has only ~7 modules
today; flatter is clearer. If the count grows past ~15 we revisit.

Coexists with `scufris_server/routes/sessions.py` — Python imports
handle the dotted distinction fine; humans get clarity from
docstrings naming the layer ("service" vs "HTTP route").

### D4: Two routers in `routes/sessions.py`

- `sessions_router` at prefix `/v1/sessions` for per-channel ops
  (GET list, POST clear-one — design has GET at `/v1/sessions`
  which collapses to the empty path under this prefix; POST goes to
  `/{channel_id}/clear`).
- `clear_router` at prefix `/v1` for the bulk op (`/v1/clear` per
  design §8 line 297).

Both are exported and appended to `ROUTERS` in
`scufris_server/routes/__init__.py`. Slight ugliness (two routers
in one module) traded against keeping all session-clearing logic
in one file. Acceptable.

### D5: `ChannelRow` shape — typed dataclass

`scufris_server.sessions.ChannelRow` is a `@dataclass(frozen=True)`
with explicit fields rather than a `sqlite3.Row` passthrough or a
TypedDict. Wins: handlers can `Field(...)` map it into pydantic
responses cleanly; mypy strict catches typos; the dataclass is the
contract between service and route layers.

```python
@dataclass(frozen=True)
class ChannelRow:
    channel_id: int
    user_id: int
    surface: str
    surface_id: str
    agent: str
    oc_session_id: str
    created_at: int     # session_link, not channel
    last_used_at: int
```

### D6: Empty user → 200 `[]`, not 404

`GET /v1/sessions?user_id=N` where user N exists but has no
channels returns `200 []`. 404 only when user N is missing entirely
(D2). Distinguishes "you have no sessions yet" from "you don't
exist." Bulk clear (`POST /v1/clear`) similarly returns `{count: 0}`
not 404 for existing users with no links.

---

## Additional micro-decisions

Recording for posterity; small but real.

- **`list_sessions()` is a single round-trip.** Even with 50+
  opencode sessions on the live host the response is small (~12KB).
  No streaming, no pagination wrapper. If N grows past ~500 we
  revisit.
- **Enrichment lookup is by oc_session_id.** Build `dict[str,
  Session]` from `list_sessions()`, then iterate DB rows. Sessions
  opencode has that we don't track (created out-of-band) are
  invisible to our GET — correct behaviour, not a bug.
- **`clear_channel_link` returns bool, not None.** Lets the route
  expose `{cleared: false}` when the channel exists but had no
  link (edge case: chat was attempted but opencode rejected, so
  `_record_new_session` never ran). 200, not 404 — the channel is
  legit; there's just nothing to clear.
- **Refactor minimalism.** Only `_record_new_session` moves to
  the service module. `_resolve_session` and `_touch_session` stay
  in chat.py as private helpers because they're chat-loop specific
  (join-then-fetch, then UPDATE on bump) and have no consumer
  outside chat.
- **No transaction across `list_sessions()` + DB read.** GET
  endpoint reads DB rows first (one txn), then calls opencode
  (no DB lock held), then merges. Opencode side-effects to its
  own session set during the call are tolerated — at worst a
  freshly-created session shows nulls in the response (race window
  on the order of a single HTTP round-trip).
- **Response field naming.** GET response uses `channel_id` (snake)
  to match `last_used_at` / `oc_session_id`. Keeps the v0 API
  consistently snake_case. Pydantic responses don't need
  `alias_generator`.
- **`POST /v1/clear` body validates `user_id` as `int >= 1`.** No
  optional fields; if a future bulk-clear filter is needed (e.g.
  "only older than X"), add an explicit endpoint or a query param.

---

## TOML schema impact

None. `config.toml` (#12) does not configure session management.
The only new env knob lands in #34 (expiry TTL); this task adds
zero new settings.

---

## Module layout

Touches:

```
scufris_server/
├── sessions.py             # NEW — service layer (D3)
├── opencode_client.py      # ADD: list_sessions(); extend Session model
├── routes/
│   ├── __init__.py         # ADD: sessions_router + clear_router to ROUTERS
│   ├── sessions.py         # FILL: two routers, three routes
│   └── chat.py             # SWAP: _record_new_session → sessions.create_channel_link
```

Tests touched:

```
tests/unit/
├── test_sessions.py        # NEW — service layer (~8 tests)
├── test_sessions_route.py  # NEW — HTTP layer (~12 tests)
├── test_chat_route.py      # UNCHANGED — chat behaviour unaffected by refactor
├── test_placeholders.py    # SHRINK — drop sessions case; ROUTERS 3 → 5
└── (no test_opencode_client changes)  # list_sessions covered via route tests

tests/integration/
└── test_sessions_real.py   # NEW — opt-in real-opencode round-trip
```

Docs touched:

```
docs/
├── 002_api_reference.md    # ADD: GET /v1/sessions, POST clear-one, POST clear-all
└── 006_development.md      # ADD: sessions service module in the layer diagram
```

---

## TODOs (in implementation order)

Each step is small enough to land in one commit and review independently.

1. **Update `tasks/20260613-091044/TASK.md`.** [this file]
   - [x] File follow-ups #33 (fork) + #34 (expire).
   - [x] Expand from 13-line stub to full scope.

2. **Extend `Session` model in `opencode_client.py`.**
   - [x] Add typed `title: str | None`, `cost: float | None`,
         `tokens: TokenUsage | None`, `time: SessionTime | None`.
   - [x] Define `SessionTime(BaseModel)` with `created: int`,
         `updated: int`, `extra="allow"`.
   - [x] No new tests file; existing chat tests + new session route
         tests exercise the parsing via mocked responses.

3. **Add `OpencodeClient.list_sessions() -> list[Session]`.**
   - [x] Wraps `GET /session` via `_request`; returns
         `[Session.model_validate(r) for r in resp.json()]`.
   - [x] Docstring mentions: returns ALL opencode-visible sessions,
         not filtered to scufris-tracked ones; filtering is the
         caller's job (intersection on `oc_session_id`).

4. **Create `scufris_server/sessions.py` service module.**
   - [x] Module docstring naming the owning task (#10).
   - [x] `ChannelRow` frozen dataclass (D5 shape).
   - [x] `create_channel_link(conn, user_id, surface, surface_id,
         agent, oc_session_id) -> int`. Behaviour matches the old
         `_record_new_session`: single transaction, INSERT channels
         + INSERT session_links, return new channel_id.
   - [x] `list_user_channels(conn, user_id) -> list[ChannelRow]`.
         Joins channels × session_links; sorted by `last_used_at
         DESC` for stable "most recent first" UX.
   - [x] `get_channel(conn, channel_id) -> ChannelRow | None`.
         Signature takes no `user_id` — ownership check belongs in
         the route (lets #33 fork reuse for parent lookup).
   - [x] `clear_channel_link(conn, channel_id) -> bool`.
   - [x] `clear_user_links(conn, user_id) -> int`.
   - [x] No FastAPI imports — pure stdlib + sqlite3 (matches
         identity.py).
   - Resolved Q1 (orphan channels): INNER JOIN everywhere; chat-
     path INSERT is atomic so orphans shouldn't occur. Documented
     in module docstring "Joining policy".
   - Resolved Q3 (sort key): `last_used_at DESC`.
   - Gates: ruff clean, mypy --strict clean, 189/189 unit pass.

5. **Unit tests for `sessions.py`.**
   - [x] `tests/unit/test_sessions.py`: create+read roundtrip,
         list ordering (last_used_at DESC), clear returns
         True/False correctly, bulk clear returns count,
         clear preserves channels row, idempotency
         (clear twice → second returns False / 0).
   - 13 tests covering all 5 functions + 2 ChannelRow shape
     assertions (frozen, value-equality). Suite runs in ~0.15s.
   - Gates: ruff clean, mypy --strict clean, 202/202 unit pass
     (was 189; +13 new).

6. **Refactor `routes/chat.py`.**
   - [x] Replace `_record_new_session` body with a call to
         `sessions.create_channel_link`.
   - [x] Keep `_resolve_session` and `_touch_session` as private
         helpers (chat-loop specific).
   - [x] Existing test_chat_route.py runs unchanged — no behaviour
         delta, just code locality.
   - `_record_new_session` deleted entirely (per acceptance
     criterion line 468), not just stubbed. Call site at the
     "new session" branch now invokes
     `create_channel_link(conn, user_id, surface, surface_id,
     agent, oc_session_id)` and discards the returned channel_id
     (chat doesn't use it; sessions.py logs it on the DB-write
     side already).
   - Added a docstring NB on `_resolve_session` flagging *why*
     the (user_id, surface, surface_id, agent) lookup stays here
     vs moving to `sessions.py` (no other caller needs it; the
     future fork in #33 uses get_channel by channel_id instead).
   - Net diff: +14/-21 lines in chat.py.
   - Gates: ruff clean, mypy --strict clean, 202/202 unit pass
     (all 18 chat-route tests pass without modification).

7. **Implement `routes/sessions.py` — GET /v1/sessions.**
   - [x] Two routers: `sessions_router` (prefix `/v1/sessions`)
         and `clear_router` (prefix `/v1`).
   - [x] `GET /` on sessions_router: query `?user_id=N`.
         Override-first authz (D2). 404 if user unknown.
         Call `list_user_channels`; if non-empty, call
         `opencode.list_sessions()` in try/except wrapping
         `OpencodeNetworkError` + `OpencodeServerError` →
         degrade to nulls (D1). Build response.
   - [x] Define response pydantic: `SessionListItem` (channel_id,
         channel: Channel, oc_session_id, last_used_at,
         title: str | None, tokens: Tokens | None, cost: float | None).
   - **Channel migration**: per user choice, `Channel` pydantic
     moved from `routes/chat.py` to `scufris_server/sessions.py`
     (service module). Routes import from there. Module
     docstring updated to acknowledge pydantic schemas live
     here (matches `identity.py` convention with `IdentityFile`
     etc.); the "no FastAPI imports" rule stays — pydantic is
     not FastAPI.
   - **Tokens reuse**: `routes/sessions.py` imports `Tokens` from
     `routes/chat.py`. Routes-importing-routes is acceptable for
     shared wire shapes; can refactor later if more endpoints
     start sharing models.
   - **Authz helper**: `_resolve_principal(conn, override,
     requested_user_id)` private to the route module.
     Order: override > requested_user_id (validated, 404 if
     unknown) > DEFAULT_USER_ID. Step 9 reuses it for the bulk
     clear endpoint.
   - **Path**: route registered as `""` (not `/`) to avoid the
     trailing-slash 307 redirect; the public URL is `GET /v1/sessions`.
   - **Mount deferred to step 10**: routers are defined but not
     in `ROUTERS` yet. `test_placeholders.py`'s
     `HTTP_PLACEHOLDERS` parametrize loses the sessions tuple
     (1 line removed; module docstring updated to acknowledge
     #10 promotion alongside #12's identity promotion); ROUTERS
     count assertion (3) untouched.
   - **Drift cleanup**: 1-line stale formatting in
     `opencode_client.py` (mine, from step 3) fixed. 6
     unrelated files have pre-existing drift; filed as
     `tasks/20260616-120606` (P20) — does not block #10.
   - Gates: ruff check clean, ruff format clean on all touched
     files, mypy --strict clean, 201/201 unit pass (was 202;
     -1 from HTTP_PLACEHOLDERS shrinking).

8. **Implement POST /v1/sessions/:channel_id/clear.**
   - [x] Look up channel via direct ``channels`` query (NOT
         ``get_channel``); 404 if unknown OR
         ``channel.user_id != resolved_user_id``.
   - [x] Override note: without an explicit user_id in the body,
         we use override-or-default-1 as the implicit principal,
         then enforce ownership via the channel row. Documented in
         the route docstring.
   - [x] Call `clear_channel_link`; return `{cleared: bool}`.
   - **Deviation from acceptance text (resolved during impl)**:
     planned to use ``sessions.get_channel`` per the original
     bullet, but that INNER JOINs and would 404 on the second
     clear (orphan channels invisible). Step 11's planned test
     "POST clear no link → cleared=false, 200" requires the
     orphan-channel case to stay observable, so the route uses
     a direct ``SELECT user_id FROM channels WHERE id=?`` query
     instead. Documented in the route docstring + ClearResponse
     docstring.
   - **404 unified body**: "channel not found" body is returned
     for both "channels row missing" and "wrong owner" so
     non-owners can't probe for channel existence (D2).
   - **Logging**: WARNING on 404 (channel_id + principal so scan
     attempts are spottable); INFO on success (cleared=true) and
     no-op (cleared=false), both with channel_id + principal +
     cleared bool extras for JsonFormatter lift.
   - **Route still unmounted**: step 10 will mount; this step
     just adds the handler to ``sessions_router``.
   - Gates: ruff clean, mypy --strict clean, 201/201 unit pass.

9. **Implement POST /v1/clear on clear_router.** DONE.
   - [x] Body: `{user_id: int >= 1}`.
   - [x] Override-first authz; 404 if user unknown.
   - [x] Call `clear_user_links`; return `{count: int}`.
   - **Models added** (`routes/sessions.py` Response-models section):
     - ``BulkClearRequest`` — single field ``user_id: int`` with
       ``Field(..., ge=1, description=...)`` for the JSON-schema
       constraint. Pydantic rejects ``user_id=0`` or absent with
       422 before our code runs.
     - ``BulkClearResponse`` — single field ``count: int``, the
       number of rows ``clear_user_links`` removed.
   - **Import**: ``Field`` added to the pydantic import (the only
     v2 helper we need beyond ``BaseModel``).
   - **Handler** ``clear_user`` on ``clear_router.post("/clear")``:
     - Takes ``payload: BulkClearRequest`` plus the standard
       ``conn`` + ``override`` deps.
     - ``principal = _resolve_principal(conn, override, payload.user_id)``
       — same helper as the per-channel clear, with the body
       ``user_id`` flowing through the override-first rule
       (override silently wins; missing user → 404; default
       fallback irrelevant here because pydantic forces ``ge=1``).
     - ``count = clear_user_links(conn, principal)`` — service
       layer commits its own ``with conn:`` block.
     - Returns ``BulkClearResponse(count=count)``.
   - **D6 in action**: zero-link case returns ``{count: 0}`` with
     200, not 404. 404 is reserved for ``payload.user_id`` not
     matching any row in ``users`` (raised by
     ``_resolve_principal`` before we touch the links table).
   - **Logging**: single INFO with ``principal`` + ``count`` extras
     on every success (including count=0). No WARNING path —
     unlike per-channel clear, the only "denied" outcome here is
     the 404 already logged inside ``_resolve_principal``'s caller
     (well, actually ``_resolve_principal`` raises HTTPException
     directly without logging; if that becomes a gap we add a
     debug log in a follow-up — not blocking #10).
   - **Route still unmounted**: step 10 will mount; this step
     just adds the handler to ``clear_router``.
   - **Naming asymmetry stays**: ``ClearResponse`` (per-channel,
     no body, ``cleared: bool``) vs ``BulkClearRequest`` /
     ``BulkClearResponse`` (bulk, has body, ``count: int``). The
     "Bulk" prefix marks the wide-blast endpoint; the per-channel
     pair stays terse since it's the more common surface. Defer
     any rename to the cleanup pass.
   - Gates: ruff clean, ruff format clean (one wrap-line trim
     after auto-format), mypy --strict clean, 201/201 unit pass.

10. **Wire routers + update test_placeholders.** DONE.
    - [x] `routes/__init__.py`: import `sessions_router` and
          `clear_router` from `routes.sessions`; append both to
          `ROUTERS`. Update docstring (placeholder note for
          sessions drops out).
    - [x] `tests/unit/test_placeholders.py`:
          drop `("scufris_server.routes.sessions", ...)` from
          `HTTP_PLACEHOLDERS`; update ROUTERS count assertion
          3 → 5 + comment.
    - **`routes/__init__.py` rewrite**:
      - Direct submodule imports for the two new routers:
        ``from scufris_server.routes.sessions import clear_router,
        sessions_router``. The old noqa-import line drops
        ``sessions``, leaving ``permissions, stats``.
      - ``ROUTERS`` reformatted from one-line to multi-line list
        (5 entries don't read well horizontally): health → identity
        → chat → sessions_router → clear_router. Order matches the
        narrative flow of the docstring; FastAPI doesn't care about
        order, but a stable ordering helps when scanning
        ``app.routes`` in tests.
      - Docstring rewritten:
        - "step 10 lands placeholder modules" → drop (those landed
          long ago; reads as stale).
        - "#12 promotes identity..." → expanded with parallel "#10
          promotes sessions, mounting two routers" stanza.
        - Two-router split note (D4) added with cross-ref to
          ``routes.sessions`` module docstring (so future
          maintainers don't try to merge them).
        - Placeholder bullet for ``routes.sessions`` removed.
    - **`test_placeholders.py` updates**:
      - HTTP_PLACEHOLDERS already shrunk to 2 in step 7 — no
        change here.
      - Loop ``for attr in ("sessions", "stats", "permissions")``
        narrowed to ``("stats", "permissions")``. The comment
        above it now mentions both #12 (identity) and #10
        (sessions) promotions in parallel.
      - ROUTERS assertion: ``len(ROUTERS) == 3`` → ``== 5``;
        message updated to enumerate ``health, identity, chat,
        sessions, clear``.
    - **Smoke check** (manual, not codified — covered by step 11
      mount test): inspected ``app.routes`` after ``create_app``
      and saw all expected ``/v1/*`` paths:
      - ``GET /v1/healthz`` (#9)
      - ``GET /v1/version`` (#9)
      - ``POST /v1/identity/resolve`` (#12)
      - ``POST /v1/chat`` (#9 chat skeleton)
      - ``GET /v1/sessions`` (this task, step 7)
      - ``POST /v1/sessions/{channel_id}/clear`` (this task,
        step 8)
      - ``POST /v1/clear`` (this task, step 9)
    - Gates: ruff clean, format clean, mypy --strict clean (19
      source files), 201/201 unit pass.

11. **Unit tests for routes.** DONE.
    - [x] `tests/unit/test_sessions_route.py` — patterns from
          `test_chat_route.py` (respx-mocked opencode, real
          sqlite via `_make_settings(tmp_path)`).
    - [x] GET happy path (one channel, opencode returns enriched
          data → typed fields populated).
    - [x] GET empty list (valid user, no channels → `[]`).
    - [x] GET 404 unknown user_id.
    - [x] GET opencode-degraded (opencode 503 / network error →
          200 with nulls + warning log).
    - [x] GET ordering (multiple channels, response sorted by
          last_used_at DESC).
    - [x] GET with override active (different user_id in query →
          response scoped to override user).
    - [x] POST clear happy path (link existed → cleared=true;
          channel row preserved).
    - [x] POST clear no link (channel exists, no link →
          cleared=false, 200).
    - [x] POST clear 404 unknown channel.
    - [x] POST clear 404 not-owned (other user's channel).
    - [x] POST /v1/clear with N links → `{count: N}` + all links
          gone.
    - [x] POST /v1/clear with zero links → `{count: 0}` (not 404).
    - [x] POST /v1/clear 404 unknown user_id.
    - [x] Mount smoke check: both routers in `app.routes`.
    - **17 tests** (one-per-bullet plus two extras): also added
      a 5xx-degradation test (alongside the network-error one,
      since D1 lumps them; both should degrade identically) and a
      ``user_id=0`` 422 test (proves the ``Field(..., ge=1)``
      constraint catches non-positive ids before our handler runs)
      and a "POST /v1/clear with override overrides body" test
      (proves D2's override-first rule isn't bypassable via the
      JSON body — was a TODO from the next-steps list).
    - **Seed helpers added** (test-internal):
      - ``_seed_user(settings, user_id, username=None)`` — apply
        migrations + ``INSERT OR IGNORE`` a users row. Picks
        ``DEFAULT_USERNAME`` for ``user_id=1`` so the lifespan's
        ``_seed_default_user`` no-ops cleanly; synthesises
        ``f"user_{N}"`` otherwise. **Idempotent**, callable both
        before TestClient (so ``Settings.user_id=N`` overrides
        validate cleanly at lifespan startup) and after.
      - ``_seed_channel(settings, user_id, *, surface=..., ...,
        last_used_at=None)`` — calls ``_seed_user`` then
        ``create_channel_link``; optionally bumps
        ``last_used_at`` for ordering tests. Returns the new
        ``channel_id``.
      - ``_seed_link_only(settings, user_id, ...)`` — inserts a
        ``channels`` row with NO ``session_links`` row. Models
        the post-clear "channel exists, link missing" state; used
        by the ``cleared=false`` idempotency test (couldn't reuse
        ``_seed_channel`` + manual delete because that'd be three
        operations for one fixture).
    - **opencode mock helpers**:
      - ``_mock_happy_boot(mock)`` — health + provider; lifespan
        gate. Same pattern as chat-route.
      - ``_oc_session_body(*, id, title, cost, tokens_in,
        tokens_out, ...)`` — one element of opencode's
        ``GET /session`` response shape. ``title`` /
        ``cost`` / ``tokens`` all default-populated; tests pass
        ``None`` to those when probing partial-data edges (none
        actually do that yet — the model accepts sparseness, so
        nothing forced the test).
    - **D1 degradation tests**: ran the same fixture with two
      different opencode failure modes (``ConnectError`` for
      network, 503 response for server-side). Both produce
      ``title=None``, ``tokens=None``, ``cost=None`` while the
      scufris-side fields stay populated — proving the
      ``except (OpencodeNetworkError, OpencodeServerError)`` block
      catches both subclasses.
    - **404 leak check**: in the not-owned test, asserted the
      detail body contains the channel id (matching the unknown-
      channel case) so a hostile caller can't distinguish the
      two outcomes by response shape.
    - **Mount smoke**: enumerates ``r.path`` across ``app.routes``
      and asserts all three of ``/v1/sessions``,
      ``/v1/sessions/{channel_id}/clear``, ``/v1/clear`` are
      present. Trip-wire for step 10 — drops in ``ROUTERS`` would
      fail this test before integration.
    - Gates: ruff clean, ruff format clean (one auto-fix wrap
      after first run), mypy --strict scufris_server clean (19
      source files), 218/218 unit pass (was 201; +17 from this
      file).

12. **Integration test + smoke example.** DONE.
    - [x] `tests/integration/test_sessions_real.py` (opt-in marker,
          mirrors `test_chat_real.py`):
          - Drive a real `/v1/chat` to create a channel + link.
          - Hit `/v1/sessions?user_id=...`; assert title/tokens
            present (opencode populated them).
          - Hit clear-one; assert `cleared=true`.
          - Hit list again; assert opencode session still exists
            in `GET /session` (scufris doesn't destroy opencode
            data).
          - Hit `/v1/clear`; assert idempotent.
    - [x] `examples/check_sessions.py` (paired demo script — user
          asked for an example alongside the integration test;
          mirrors `check_chat.py`'s spawn-server pattern).
    - **Integration test (`tests/integration/test_sessions_real.py`)**:
      - Single test, 11-step sequence — same "one big test"
        rationale as `test_chat_real.py`: cold-start qwen3
        dominates runtime, so bundling all behaviours into one
        TestClient context is cheaper than splitting.
      - Wall-clock budget 120s (vs chat's 90s — we have 2 chats +
        6 list/clear calls; cleared the bar at 37.86s on the
        local box).
      - Reuses `opencode_url` + `ollama_default_model` fixtures
        from `tests/integration/conftest.py` (so the module skips
        cleanly when opencode is unavailable).
      - **ADR-13 verification** (the headline assertion the task
        asked for): a `_opencode_session_ids(opencode_url)`
        helper hits opencode's `GET /session` directly via
        `httpx.get` and returns the set of ids. Called twice
        post-clear (once after per-channel clear, once after
        bulk clear) and asserts both seeded session ids are still
        in opencode's set. Proves scufris's clears never touch
        the upstream session.
      - Per-channel idempotency: clear→`cleared=true`,
        clear→`cleared=false`. Bulk idempotency:
        clear→`count=1`, clear→`count=0`.
      - `httpx` already a project dep (chat path uses it); no
        new dep needed for the side-channel probe.
    - **Smoke example (`examples/check_sessions.py`)**:
      - Same shape as `check_chat.py`: spawn `python -m
        scufris_server` on a free port, pre-flight `/v1/healthz`,
        then drive the surface and assert.
      - 9-step sequence: pre-flight → seed A → seed B → list (2,
        enriched) → clear A → list (1) → clear A retry (`cleared:
        false`) → bulk clear → list (0) → bulk clear retry
        (`count: 0`).
      - HTTP helpers (`_get`, `_post`, `_show`) stay inline —
        each `examples/` script is supposed to be self-contained
        per `examples/README.md` (no shared toolbox in this
        directory yet; revisit when the next script needs the
        same shapes).
      - `_post` accepts `payload=None` for the per-channel clear
        endpoint, which takes no body.
      - Ran live: 30s end-to-end (2 cold qwen3 chats + 7 fast
        calls). Output prints labelled sections with
        ``X-Request-Id`` per call so a debugger can correlate
        with the server log.
    - **README inventory** (`examples/README.md`):
      - Added a `check_sessions.py` row to the table.
      - Added the runner line under "Quick start".
    - **Live verification (live opencode at 4096)**:
      - `pytest -m integration tests/integration/test_sessions_real.py`
        passed in 37.86s.
      - `python examples/check_sessions.py` exited `OK`. Real
        title-enrichment captured (`"Pong reply"`, `"Hi /think"`)
        and `cost=0.0` confirmed numeric (ollama is free).
    - Gates: ruff clean, format clean (one auto-format on
      `check_sessions.py` after first run), mypy --strict
      scufris_server clean, plain mypy on the two new files
      clean, 218/218 unit pass (untouched), 1/1 integration pass.
    - **Follow-up — env pin (post-merge tweak):** during walkthrough
      noticed the example's identity resolution was fragile —
      `surface="example"` doesn't match any standard TOML map, so
      it falls through to the default user (1) only by accident,
      and an exported `SCUFRIS_USER_ID` in the caller's env would
      leak through. Pinned the spawned server's env with
      `"SCUFRIS_USER_ID": str(DEFAULT_USER_ID)` (= "1") and updated
      the `DEFAULT_USER_ID` comment to document the override
      mechanism. Re-ran ruff (clean, no reformat needed) and the
      example end-to-end (~30s, exits `OK`). Output identical
      since user 1 was already the resolved principal — the pin
      hardens determinism, not behaviour.

13. **Live verification on the running server.** — **SKIPPED.**
    Steps 11-12 already exercise the full sessions surface against
    a live stack: unit-route tests cover all 13 spec bullets + 4
    extras with mocked opencode, the integration test
    (`tests/integration/test_sessions_real.py`, ~38s) drives the
    real opencode at 4096 and verifies the ADR-13 "scufris doesn't
    destroy" invariant by snapshotting `GET /session` before/after
    each clear, and `examples/check_sessions.py` walks 9 endpoint
    calls end-to-end against the real stack and exits `OK` in
    ~30s. A separate manual curl walkthrough on port 7082+ would
    re-run the same calls the example already runs. User
    confirmed skip (recommended option).
    - [x] N/A — covered by steps 11 + 12.

14. **Docs updates.**
    - [x] `docs/002_api_reference.md`: add the three endpoints with
          example request/response bodies; note D1 degradation
          contract; cross-reference #33 / #34 as "not yet shipped."
    - [x] `docs/006_development.md`: add `sessions.py` to the
          service-layer listing alongside `identity.py`.
    - [x] `docs/005_data_model.md`: if it doesn't already mention
          the clear semantics ("clear drops session_links only,
          opencode session persists"), add a short paragraph.

    **Completion notes:**
    - **002_api_reference.md**: headline table rewritten (sessions
      placeholder row → 3 live rows; trimmed `/v1/stats/*` purpose
      since `/v1/clear` is no longer "stats territory"; added a
      `not yet (#33)` row for fork). Three new endpoint sections
      slotted between `/v1/chat` and "Placeholder routers", each
      with the standard layout (Source → Request → Response → Side
      effects → Errors → Examples). Covers D1 degradation, D2
      override-wins authz, D6 idempotent count=0, ADR-13 ("scufris
      never destroys upstream sessions"), unified 404 body for the
      per-channel clear, and INNER JOIN scoping (channels-without-
      a-link deferred). Pruned the placeholder routers table —
      `routes/sessions.py` is gone, `/v1/stats` planned endpoints
      trimmed to `GET /v1/stats` only. "Future endpoints" section
      expanded with fork (#33, server-mints `surface_id` per D1)
      and expire (#34) bullets. Net delta: ~+230 lines, -10 lines.
    - **005_data_model.md**: surgical edits — line 159 fork ref
      now points at `#33`; lines 175-176 + 199-202 swap
      `chat._record_new_session()` → `sessions.create_channel_link()`
      with the right `sessions.py:175` line ref; the "Will be
      deleted by the (future) ..." sentence promoted to live tense
      and cross-referenced to 002. ADR-13 paragraph (existing) was
      already correct — left alone. Transaction-policy bullet at
      line 344 also updated to remove the dead `_record_new_session`
      mention.
    - **006_development.md**: project layout block now lists
      `sessions.py # Channel ↔ opencode-session service layer`.
      Tests-table integration row generalised: ~190+ → ~220 unit
      tests, integration listing now mentions both
      `test_chat_real.py` and `test_sessions_real.py`. Per-test
      ceilings clarified: 90s chat, 120s sessions.
    - **Out of scope (flagged for follow-up if user wants):**
      `docs/001_architecture.md` lines 194 + 231 still reference
      the dead `_record_new_session()` helper. Two literal
      one-line swaps; not done in this step because 001 wasn't on
      the originally-approved scope list. User can choose to fold
      it into step 15 cleanup or punt to a separate doc-tidy task.
    - Gates: ruff clean on `scufris_server` + `examples`; ruff
      format drift in 6 files of `tests/unit` is pre-existing
      (tracked in `tasks/20260616-120606`, not introduced here).
      No Python files touched — so unit/integration counts
      unchanged at 218/218 + 1/1.

15. **Cleanup + sign-off.**
    - [x] `uv run --active pytest tests/unit` green — 218 passed in 3.41s.
    - [x] `uv run --active pytest -m integration tests/integration`
          green (live opencode required; skip if absent) — 2 passed
          in 44.40s (`test_chat_real.py` + `test_sessions_real.py`).
    - [x] `uv run --active ruff check scufris_server tests` clean.
    - [x] `uv run --active mypy --strict scufris_server` clean —
          19 source files, no issues.
    - [x] Mark STATUS: DONE here with closing notes (final test
          counts, live-verification artefacts, follow-ups if any).

    **Closing notes:**
    - **Final test counts:** 218 unit (was 201 pre-#10; +17 from
      `test_sessions_route.py`, +13 from `test_sessions.py`,
      cancelled 13 in test_placeholders cleanup → net +17 mounted
      under tests/unit). 2 integration (chat 1 + sessions 1).
    - **Live-verification artefacts:**
      - `tests/integration/test_sessions_real.py` — single
        bundled test, 11-step seed → list → clear-A → list →
        ADR-13 invariant → bulk-clear → list → bulk-retry. Wall
        clock 37–42s on warm cache.
      - `examples/check_sessions.py` — paired demo
        spawning a fresh server on a free port, 9-step flow,
        exits `OK` in ~30s with real `cost=0.0` and titles
        `"Pong reply"` / `"Hi /think"` (or similar — depends on
        what qwen3 picks for the title that day). Pinned via
        `SCUFRIS_USER_ID=1` so identity resolution is
        host-independent.
    - **Schema impact:** none. The 1:1 `channels` × `session_links`
      schema established in #9 step 8 carried through unchanged.
    - **Production deviations:**
      - The per-channel clear handler queries `channels` directly
        (rather than via `sessions.get_channel`, which INNER-JOINs
        `session_links`) so that the post-clear `cleared=false`
        idempotent case stays observable. Documented in
        `routes/sessions.py:323-329` and step 8 completion notes.
      - `BulkClearRequest` / `BulkClearResponse` use the
        Request/Response naming pair, while the per-channel uses
        bare `ClearResponse` (no Request type — body-less). Mild
        asymmetry accepted to keep the per-channel handler
        body-less and the bulk handler body-bearing.
      - `SCUFRIS_USER_ID` override resolution wins over both query
        params and request bodies (D2). The bulk endpoint's
        `user_id` field is required by the schema (validation),
        but silently overridden when the env var is set — this
        is intentional, prevents pinned operators from accidentally
        clearing a different user via stray curl.
    - **Follow-ups filed:**
      - **#33** (`tasks/20260616-111428`, P70) — fork endpoint.
        Hard-blocks broken now that `sessions.py` exists and is
        importable. Server-mints `parent.surface_id + "#" + new_oc_id[:8]`.
      - **#34** (`tasks/20260616-111430`, P50) — clear-with-expiry
        variant that also DELETEs upstream opencode session.
        Distinct from `/v1/clear` precisely because `/v1/clear`
        upholds ADR-13. Naming TBD.
      - **`tasks/20260616-120606`** (P20) — pre-existing ruff
        format drift in 6 files of `tests/unit/` (test_identity.py,
        test_identity_route.py, test_logging.py, …). Not
        introduced by #10 — surfaced when running broad
        `ruff format --check`. Standalone tidy task.
    - **Out of scope from #10 (deferred — not blocking):**
      - Q2: include `bound_surfaces` in `GET /v1/sessions` rows.
        Postponed pending a use case.
      - "List bound surfaces that have never chatted" (LEFT JOIN
        of `surface_bindings` × `channels`) — deferred for the
        same reason; today's INNER JOIN is sufficient.
      - Renaming `ClearResponse` → `SingleClearResponse` for
        symmetry with `BulkClearResponse`. Mild churn, not now.
    - **Path A continuation:** next is **#11 SSE streaming**
      (`tasks/20260613-091045`, P75). After that, **#14 CLI v2**
      (P80). **#28 observability** still slotted opportunistically.

---

## Acceptance criteria

- [x] `GET /v1/sessions?user_id=N` returns each of user N's channels
      with channel descriptor, oc_session_id, last_used_at, and
      opencode-supplied title/tokens/cost.
- [x] When opencode is unreachable, the same endpoint still returns
      200 with title/tokens/cost as null + a warning log.
- [x] `POST /v1/sessions/:channel_id/clear` drops the
      `session_links` row, preserves the `channels` row, returns
      `{cleared: true}` (or `false` if no link existed).
- [x] `POST /v1/clear {user_id}` drops every `session_links` row
      for that user and returns `{count: N}`.
- [x] Opencode-side sessions are unchanged after any clear (verified
      in the integration test by comparing `GET /session` before /
      after).
- [x] `routes/chat.py` no longer carries `_record_new_session`
      directly; that helper is replaced by a call to
      `sessions.create_channel_link`. All existing chat tests still
      pass.
- [x] `SCUFRIS_USER_ID` override pins all three endpoints to the
      override user regardless of client-supplied user_id (mirrors
      chat / identity behaviour).
- [x] All existing unit tests still pass (189 → 218); new tests
      cover service helpers + routes + degradation + authz.
- [x] `ruff check` + `mypy --strict scufris_server` clean.
- [x] `docs/002_api_reference.md` documents the three endpoints.

---

## Dependencies & sequencing

- **Hard-blocks**: #9 (`20260613-091043`, DONE) for the server
  skeleton + `channels` / `session_links` schema; #12
  (`20260613-091046`, DONE) for the identity layer powering
  override-first authz.
- **Soft-depends-on**: nothing else. Design doc (091036) is CLOSED.
- **Unblocks**:
  - #11 (`20260613-091045`, SSE) inherits this task's session
    resolution (already in chat, now factored through the service
    module).
  - #14 (`20260613-091049`, CLI) can call the new endpoints to
    drive `/sessions` and `/clear` slash commands.
  - #33 (`20260616-111428`, fork) and #34 (`20260616-111430`,
    expire) both hard-block on the `scufris_server/sessions.py`
    module this task creates.

---

## Open questions

Small implementation-time decisions; none block starting.

1. ~~**Should `list_user_channels` include channels without
   session_links?**~~ **Resolved (step 4):** INNER JOIN. The chat-
   path create wraps both INSERTs in one transaction so orphans
   shouldn't occur; if one ever does (operator edit, future bug),
   keeping it invisible is the conservative v0 choice. `ChannelRow.
   oc_session_id` stays non-Optional. See `sessions.py` module
   docstring "Joining policy."
2. **Should `bound_surfaces` (#12 response shape) influence the
   list response?** Currently no — sessions list is purely
   channel-centric. Could enrich each channel row with the user's
   bound_surfaces for context, but that's UI-layer concern. Lean:
   skip in v0; CLI/Telegram fetch identity once and cache.
3. ~~**Sort key for GET list.**~~ **Resolved (step 4):**
   `last_used_at DESC`. Matches "most recent activity" UX;
   `created_at` would freeze ordering at channel-birth time.

---

## Resolution log

Filled in as each step lands.

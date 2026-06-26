# scufris-cli v2: REPL client over HTTP

- STATUS: OPEN
- PRIORITY: 80
- TAGS: cli,client

Two new packages — `scufris_client` (async SDK over the v2 HTTP
surface) and `scufris_cli` (terminal REPL that consumes it) — giving
the user functional parity with v1's `scufris-cli` against the new
server. Same UX (rich-rendered thinking trail, slash commands,
multiline input, persistent history), new transport (`POST
/v1/chat/stream` SSE replaces v1's in-process LangChain runtime).

The SDK is a faithful port of `feature/opencode:scufris_client/client.py`
(363 LoC) trimmed to the v2 endpoint set (every live `/v1/*` route
*except* the non-streaming `/v1/chat` — see D2). The CLI is a faithful
port of `feature/opencode:cli.py` (484 LoC) trimmed to the v2 event
taxonomy (no `compaction`, no `tool_meta.prior_turns`/`context`; new
first-class `tool_result` branch — see D5) plus a new `/sessions`
command (D4).

Implements:

- Design doc `tasks/20260613-091036/TASK.md` §6 (cli surface) and §8
  (`POST /v1/chat/stream`) consumer side. The server-side endpoints
  landed in #10 (sessions / clear), #11 (chat / chat-stream), #12
  (identity / config), #9 (healthz / version); this task is the
  first-party client that exercises all of them.
- Replaces the empty `cli.py` stub at the repo root (currently
  `scufris-cli = "cli:main"` in `pyproject.toml`).
- Pre-step 0 doc-bug fix in `docs/002_api_reference.md`: the SSE
  `kind` table claimed `text_delta` / `reasoning` (v1-era names);
  the v2 mapper actually emits `text` / `tool_call` / `tool_result`
  / `tool_meta`. Fixed in step 0; CLI port needs the renderer to
  match the *real* server output.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).

---

## Scope

### In

1. **`scufris_client/` package** (NEW, two files):
   - `scufris_client/__init__.py` — re-exports the public surface
     (`ScufrisClient`, `StreamEvent`, `ThinkingEvent`, all four error
     classes) so callers can `from scufris_client import ...`
     without reaching into private modules.
   - `scufris_client/client.py` — async `ScufrisClient` with
     `httpx.AsyncClient` under the hood. Methods:
     - `__aenter__` / `__aexit__` for `async with` lifecycle.
     - `healthz() -> dict` — GET /v1/healthz.
     - `version() -> dict` — GET /v1/version.
     - `resolve_identity(surface, surface_id) -> dict` —
       POST /v1/identity/resolve.
     - `chat_stream(surface, surface_id, agent, message) ->
       AsyncIterator[StreamEvent]` — POST /v1/chat/stream; yields
       typed `StreamEvent` instances (D12).
     - `sessions(user_id) -> list[dict]` — GET /v1/sessions.
     - `clear_session(channel_id) -> dict` — DELETE
       /v1/sessions/{channel_id}/clear.
     - `clear(user_id) -> dict` — DELETE /v1/clear.
   - Module-level dataclasses:
     - `StreamEvent` — discriminator dataclass with
       `kind: Literal["thinking", "done", "error"]`. For
       `thinking`: carries a `ThinkingEvent`. For `done`: carries
       `text`, `oc_session_id`, `oc_message_id`, `tokens`, `cost`.
       For `error`: carries `error`, `error_type`. See D12.
     - `ThinkingEvent` — verbatim wire-compat with the server's
       `scufris_server.events.ThinkingEvent`: 9 fields (`kind`,
       `source`, `text`, `depth`, plus optional `arg`, `context`,
       `prior_turns`, `evicted`, `new_facts`). See D13.
   - Exception hierarchy:
     - `ScufrisError(Exception)` — base.
     - `ScufrisConnectionError(ScufrisError)` — transport failure
       (cannot reach the server).
     - `ScufrisAuthError(ScufrisError)` — 401/403 (reserved; v2
       loopback is unauth today per ADR-8).
     - `ScufrisServerError(ScufrisError)` — 5xx with the server's
       structured body if any.
   - Internal `_parse_sse_stream(response)` async-iterator helper
     that consumes the response body chunk-by-chunk, splits on
     `\n\n`, and yields parsed `(event_name, payload_dict)` pairs.
     Comment lines (`:` prefix — keepalives) are dropped.
   - `_json(method, path, **kwargs)` HTTP helper that maps
     transport errors to `ScufrisConnectionError`, 401/403 to
     `ScufrisAuthError`, 4xx to `ScufrisError`, 5xx to
     `ScufrisServerError`.

2. **`scufris_cli/` package** (NEW, two files):
   - `scufris_cli/__init__.py` — empty marker file; entry point
     imports from `.main`.
   - `scufris_cli/main.py` — full REPL. Structure faithfully ports
     `feature/opencode:cli.py`:
     - `HISTORY_FILE = Path.home() / ".scufris_cli_history"`
     - `HELP_TEXT` constant with all six commands.
     - `_setup_readline()`, `_save_readline_history()`,
       `_read_input(console, multiline)`.
     - `make_render_thinking(console, settings)` — closure returning
       a render function that switches on `ev.kind`. Branches:
       `tool_call` (cyan arrow + arg), `tool_result` (green check
       or red prefix on `<tool> failed: ...`), `tool_meta` (dim
       grey), `text` (dim grey delta). NO `compaction`, NO
       `tool_meta.prior_turns`/`context`, NO `is_sub_agent` verb
       split — see D5.
     - `_handle_message(console, client, surface_id, message,
       render_thinking, logger)` — drives `chat_stream`, renders
       thinking events live, panel-prints the final reply.
     - `_handle_command(console, client, user_id, surface_id, cmd,
       multiline, settings) -> (should_exit, new_multiline)` —
       slash dispatch for the six commands (D4).
     - `_amain(args)` — argparse parsing, identity resolve,
       banner, main loop.
     - `main()` entry point with `asyncio.run`.

3. **Environment variable surface** (D7, D8):
   - `SCUFRIS_SERVER_URL` — base URL. Default
     `http://127.0.0.1:7080` (v2 default port; v1 used 8765).
   - `SCUFRIS_USER` — surface_id sent to /v1/identity/resolve.
     Falls back to `getpass.getuser()`.
   - `SCUFRIS_FULL_THINKING` — `"1"` → start in full mode (default),
     `"0"` → start in short mode. `--short-thinking` argparse flag
     overrides.
   - **NOT** carried from v1: `SCUFRIS_USER_ID`, `SCUFRIS_TOKEN`.
     The first is no longer needed (server resolves user_id from
     surface+surface_id); the second is reserved for when auth
     lands (out of scope per D2 / ADR-8).

4. **Test coverage** (D6):
   - `tests/unit/test_scufris_client.py` — ~20 tests, respx-mocked:
     each public method's happy path, error mapping for each
     status class, SSE parsing of a multi-event stream (thinking
     → thinking → done; thinking → error), comment-line drop,
     `async with` lifecycle, default URL/env handling.
   - `tests/unit/test_scufris_cli.py` — ~12 tests with a mocked
     `ScufrisClient`: render branch per `kind`, slash-command
     dispatch table, multiline toggle, identity-resolve fallback
     when server returns error, banner-string format,
     `--short-thinking` arg precedence.
   - `tests/integration/test_cli_real.py` (NEW, 90s budget,
     opt-in `-m integration`) — spawns a real `scufris-server`
     subprocess + uses the real `ScufrisClient` (NOT the CLI as a
     subprocess — too brittle to drive a terminal). Sends one
     "what is 2+2?" turn; asserts at least one `thinking` event
     arrives, `done` carries `text` containing "4",
     `oc_session_id` non-empty.

5. **Docs**:
   - `docs/000_overview.md` — move "scufris-cli (#14)" from
     "Not implemented yet" to "Implemented today".
   - `docs/001_architecture.md` — module map gets `scufris_client/`
     + `scufris_cli/` rows; new short "Client packages" section
     explaining the SDK / REPL split.
   - `docs/006_development.md` — layout block gains both packages;
     tests table bumps unit count (+~32) and integration count
     (+1); backlog table marks #14 closed.
   - `docs/002_api_reference.md` — only the step 0 doc-bug fix
     (already landed); no further changes (CLI is a consumer, not
     a wire-format change).

6. **pyproject.toml** (D16 — user edits manually at step 9):
   - Add `scufris_client`, `scufris_cli` to
     `[tool.hatch.build.targets.wheel] packages` and
     `only-include`.
   - Change `scufris-cli` script entry point from `"cli:main"` to
     `"scufris_cli.main:main"`.
   - Optionally delete the empty `cli.py` stub at repo root (or
     leave; doesn't affect runtime once entry point is repointed).

### Out

| Feature                                              | Owning task / note                                       |
|------------------------------------------------------|----------------------------------------------------------|
| `--agent` flag and `/agent` slash command            | Future task — D3 hardcodes `"build"` for v0. Adding flag |
|                                                      | + command + persistence is one focused follow-up.        |
| `--surface`/`--surface-id` argparse flags            | Future task — D7 uses `SCUFRIS_USER` env only. Flags    |
|                                                      | would be useful for testing arbitrary identities; defer  |
|                                                      | until that need is real.                                 |
| Bearer-token auth (`SCUFRIS_TOKEN`)                  | Out — server is loopback-unauth (ADR-8). Add when /    |
|                                                      | if auth lands server-side.                               |
| `/stats` command                                     | #13 stats (`20260613-091047`, P75 OPEN) — placeholder   |
|                                                      | endpoint not implemented; reintroducing the CLI command  |
|                                                      | belongs to #13.                                          |
| `/fork` and `/expire` commands                       | #33 (`20260616-111428`, P70) / #34 (`20260616-111430`, |
|                                                      | P50) — server endpoints don't exist yet.                 |
| Permission ask/reply UX (the interactive prompt)     | #30 permissions (`20260613-093108`, P70 OPEN). #11 only |
|                                                      | tunnels `permission.updated` as `tool_meta`; the         |
|                                                      | interactive ask/decision RPC is #30. CLI renders         |
|                                                      | tool_meta as dim notes for now.                          |
| Telegram-side renderer                               | Future task — same `ThinkingEvent` wire shape; Telegram |
|                                                      | bubble renderer is a separate surface.                   |
| Non-streaming `/v1/chat` SDK method                  | D2 — stream-only on the client. Users who need the      |
|                                                      | sync shape can talk to `/v1/chat` directly with httpx;  |
|                                                      | not the CLI's job.                                       |
| Manual example script (`examples/check_cli.py`)      | D6 — integration test + live REPL run cover what the    |
|                                                      | example would. Skip the manual script.                   |
| CLI subprocess-driven integration test               | Out — driving a REPL through stdin is brittle. The     |
|                                                      | integration test exercises `ScufrisClient` end-to-end   |
|                                                      | through the SDK, which is what the CLI invokes.          |
| Replace `utils/__init__.py` content                  | Out — `utils/` is empty in v2 and will likely be       |
|                                                      | dropped entirely in #27 (v1 dep strip). Don't reseed    |
|                                                      | it here.                                                 |

---

## Design decisions (locked during scoping, 2026-06-20)

### D1: Two packages — `scufris_client` + `scufris_cli`

The SDK lives in `scufris_client/`; the REPL lives in `scufris_cli/`.
Matches v1's split (where `scufris_client/` and `cli.py` were peers).

Rationale: the SDK is reusable (tests, future integrations, ad-hoc
scripts); the CLI is one consumer. Keeping them separate lets a
third caller — Telegram-side renderer, future GUI, automation
scripts — depend on `scufris_client` without dragging in `rich` and
`readline`. The 1-file-CLI shape stays comfortably under 500 LoC
(v1 was 484); splitting the CLI internally is premature.

Rejected: single `scufris_cli` package with the SDK as a submodule.
Tighter namespace, but obscures the reusable-SDK story and would
need refactoring if Telegram lands as a peer surface.

### D2: SDK is comprehensive over the live v2 endpoint set

The SDK exposes every endpoint mounted under `/v1/*` in
`scufris_server/routes/__init__.py` (ROUTERS=6 → 8 paths) *except*
the non-streaming `POST /v1/chat`. Endpoints covered:

| Endpoint                                  | SDK method                |
|-------------------------------------------|---------------------------|
| GET `/v1/healthz`                         | `healthz()`               |
| GET `/v1/version`                         | `version()`               |
| POST `/v1/identity/resolve`               | `resolve_identity()`      |
| POST `/v1/chat/stream`                    | `chat_stream()`           |
| GET `/v1/sessions`                        | `sessions()`              |
| DELETE `/v1/sessions/{channel_id}/clear`  | `clear_session()`         |
| DELETE `/v1/clear`                        | `clear()`                 |

Rationale: even though the v0 CLI only uses chat_stream + identity
+ clear + sessions, the SDK is the *only* first-party HTTP client
the server has; rounding it out now is cheap and saves a "wait,
how do I call X?" rework when a script or a follow-up surface
needs the other endpoints.

`/v1/chat` is omitted because the user explicitly chose
stream-only at the client. Callers who need the sync shape can hit
the endpoint directly with `httpx` — not the SDK's job to provide
two parallel chat methods.

Rejected: chat-stream-only SDK. Reasonable for "first the CLI,
then the rest later" but every endpoint is a 5-15 LoC method;
deferring saves nothing and creates a gap where the obvious
`from scufris_client import ScufrisClient` user has no way to,
e.g., check version compatibility.

### D3: Agent hardcoded to `"build"`; no `--agent` flag for v0

`chat_stream(...)` calls are made with `agent="build"` baked into
the CLI; no argparse flag, no slash command, no per-turn override.

Rationale: opencode only ships the `build` agent in the default
config and that's the one the user has running. Adding flags now
requires picking a list (where? `/agent ls` against opencode?
hard-coded?) and storage shape (per-channel? per-launch? persist?)
that are out of scope for "land the CLI v2". Splitting `/agent`
into its own follow-up keeps this task focused.

Rejected: `--agent build` argparse flag with the default sourced
from config. Looks tidy but requires the config wiring, validation
("is `<agent>` a known opencode agent? do we ask opencode? do we
trust the user?"), and `/agent` slash command for live switching.
Each of those is a real design call; bundle as a future task.

### D4: Slash command set is `/help, /clear, /sessions, /thinking, /multiline, /exit, /quit`

The CLI ships exactly six user-visible slash commands. Differences
from v1:

- **Dropped:** `/stats` — v2 stats endpoint is a placeholder (#13).
  Re-adding the command is #13's job.
- **Added:** `/sessions` — lists every channel the resolved user
  owns, with id, surface, surface_id, agent, last-used time.
  Surfaces v2's channels split (which v1 didn't have — v1 had a
  single sessions table per user).
- **Carried:** `/help`, `/clear`, `/thinking [full|short]`,
  `/multiline`, `/exit`, `/quit`.

`/clear` semantics: see D11.

Rationale: matches v1 UX where surfaces still apply; adds the one
v2-specific surface (`/sessions`) to make multi-channel state
discoverable.

Rejected: keep `/stats` as a stub that prints "not yet
implemented". Adds a dead command and a code path; user can do
`curl /v1/stats` if they want to see the placeholder.

### D5: Renderer is the v1 port minus v2-dead branches; `tool_result` promoted

`make_render_thinking()` is a near-verbatim port of
`feature/opencode:cli.py:128-167` with three changes:

1. **Drop the `compaction` branch.** v2's mapper never emits
   `kind="compaction"` (server-side `events.py:140` reserves the
   Literal for v1 wire-compat; no code path produces it).
2. **Drop `tool_meta.prior_turns` / `context` rendering.** v2's
   mapper never populates either field (see
   `scufris_server/event_mapping.py:161-168` for `tool_meta` and
   `:218-235` for `tool_call` — `context` is unset everywhere).
   The dataclass keeps the fields for wire compat (D13), but the
   renderer drops the `if ev.prior_turns:` / `if ev.context:`
   conditionals.
3. **Promote `tool_result` to a first-class branch.** v1 had it as
   a fallback `else` with a `# currently unused` comment. v2's
   mapper emits one `tool_result` per completed/error tool
   (`event_mapping.py:244-265`). The new branch renders success
   as `↩ <tool>` in green, error (text starts with `<tool> failed:`)
   in red.

Also drop the `is_sub_agent()` "asks"/"uses" verb distinction —
v2 has no sub-agents (depth is always 0; opencode owns the agent
hierarchy). Hardcode "uses".

Rationale: port the look-and-feel exactly so the user's muscle
memory carries; trim dead code paths so the renderer's branches
match what the server actually emits.

Rejected: keep `compaction` / `prior_turns` branches as
defensive no-ops. They'd never fire in v2; carrying them invites
"why is this here?" cognitive overhead.

### D6: Test coverage is unit + 1 integration; no manual example

Unit tests cover the SDK (~20 tests, respx-mocked) and the CLI
(~12 tests, mocked SDK). One integration test exercises the SDK
end-to-end against a real spawned server.

Rationale: the CLI's behaviour is the slash-dispatch +
renderer-branch table; both are pure functions of inputs and
trivially unit-testable. The SDK's behaviour is HTTP shape; respx
covers it. The one integration test pins the SDK actually talks to
the server (vs the mock-only unit suite). A manual example
duplicates the integration test's setup without adding coverage —
the user can `uv run scufris-cli` against a live server to do the
same thing interactively.

Rejected: `examples/check_cli.py` script following the #11
precedent. The integration test is the machine-checked version of
that script; the live CLI itself is the human-checked version. No
gap.

### D7: Identity surface — `SCUFRIS_USER` env → `getpass.getuser()`; no `SCUFRIS_USER_ID`

`surface_id` is sourced in order:

1. `os.environ.get("SCUFRIS_USER")` if set and non-empty.
2. `getpass.getuser()` otherwise.

There is no `SCUFRIS_USER_ID` integer override.

Rationale: v2 resolves user_id from (surface, surface_id) on the
server side. The CLI doesn't need to know its own user_id to chat
(the chat request carries the channel descriptor; the server
resolves). The CLI does need user_id for `/clear` and `/sessions`
— see D10 for how it gets one.

v1's `SCUFRIS_USER_ID` shortcut existed because v1 had no
identity-resolve endpoint; the CLI computed user_id locally via
`user_id_for()` (a stable hash). v2 has resolve; the env override
adds zero value and lets users send mismatched ids to /clear vs
chat (chat resolves from surface_id; /clear from the env value).

Rejected: keep `SCUFRIS_USER_ID` for back-compat. Documented as
unused; encourages drift between env and config.

### D8: Default `SCUFRIS_SERVER_URL` is `http://127.0.0.1:7080`

v2's `scufris-server` defaults to port 7080 (per the v2 spawn
docs); v1 used 8765. The CLI follows the server's default.

Rationale: anyone running both side-by-side during transition can
point the v2 CLI at the v2 server with no env tweak. The v1 CLI's
default still works against the v1 server (different env-var
file).

Rejected: keep 8765 for muscle memory. Requires the user to set
`SCUFRIS_SERVER_URL` on every fresh shell during transition;
breaks the zero-config flow.

### D9: Identity-resolve failure → degrade, not bail

If `POST /v1/identity/resolve` fails (network / 5xx), the CLI
logs a WARNING, sets the banner to "user unknown", and continues
the REPL loop. Chat still works because the chat request carries
`channel: {surface, surface_id, agent}` and the server resolves
user_id from those internally. `/clear` and `/sessions` print a
hint that identity is required and skip.

Rationale: a transient resolve hiccup shouldn't lock the user out
of chat. The two slash commands that genuinely need user_id (state
management) degrade gracefully — and the dominant use case is
chat anyway.

Rejected: bail on resolve fail. Fast-fail is brittle for a
sometimes-flaky lifespan path; the user loses the entire session
because of one bad probe.

### D10: Cache `user_id` at startup; no per-command re-resolve

The CLI calls `resolve_identity()` once during `_amain()` startup
and stores the returned `user_id` in a local variable for the
duration of the REPL session. `/clear` and `/sessions` consume
the cached value.

Rationale: identity is stable for a CLI session (env doesn't
change mid-process). Re-resolving on every command adds a round
trip for no benefit. v1 did the same.

Rejected: re-resolve before each `/clear` / `/sessions`. Would
handle a server-side identity rebind mid-session (the resolved
user_id changes between command invocations), but that's not a
real use case for v0.

### D11: `/clear` clears every channel for the resolved user

`/clear` (with no argument) issues `DELETE /v1/clear?user_id=X`,
which drops *every* channel for user_id X (and their session
links — opencode sessions preserved per ADR-13).

Rationale: matches v1's `/clear` effect (one user = one chat
state, wipe it all). v2 splits state per channel; a v1-style
`/clear` translates to "wipe all my channels". A "clear just this
channel" command would need either (a) the CLI to know its own
channel_id (requires a list-then-find), or (b) a new endpoint
that takes the channel descriptor — both more complex than is
warranted for v0.

Rejected: `/clear` clears only the current CLI channel; add
`/clear all` to mean "everything". Too much choice for the
muscle-memory command; the listing UI is `/sessions`, then a
future `/clear <id>` for surgical operations.

### D12: `StreamEvent` is a discriminator dataclass with three kinds

`scufris_client.StreamEvent` is a single dataclass with:

```
kind: Literal["thinking", "done", "error"]
thinking: ThinkingEvent | None   # populated only for kind="thinking"
text: str | None                 # populated only for kind="done"
oc_session_id: str | None        # populated only for kind="done"
oc_message_id: str | None        # populated only for kind="done"
tokens: dict[str, int] | None    # populated only for kind="done"
cost: float | None               # populated only for kind="done"
error: str | None                # populated only for kind="error"
error_type: str | None           # populated only for kind="error"
```

`chat_stream()` yields these. Callers switch on `ev.kind`. Matches
v1's pattern (`feature/opencode:scufris_client/client.py:80-110`
`StreamEvent` is the same shape).

Rationale: avoids forcing callers to learn three response types
plus a union; one dataclass + one match. The Optional-fields
pattern is awkward but well-precedented (`pydantic` does this
under the hood). Callers always know which fields are present
because they just matched on `kind`.

Rejected: three sibling dataclasses (`ThinkingStreamEvent`,
`DoneStreamEvent`, `ErrorStreamEvent`) with a `StreamEvent` Union.
More type-safe (mypy narrows on `isinstance`); two extra symbols
in the public namespace; awkward to write the iterator's return
type as `AsyncIterator[ThinkingStreamEvent | DoneStreamEvent |
ErrorStreamEvent]`. Discriminator dataclass is the v1 precedent
and works in practice.

### D13: `ThinkingEvent` mirrors the server's wire shape exactly

`scufris_client.ThinkingEvent` is a verbatim port of
`scufris_server.events.ThinkingEvent`: same 9 fields (`kind`,
`source`, `text`, `depth`, `arg`, `context`, `prior_turns`,
`evicted`, `new_facts`). Same `kind` Literal that includes
`"compaction"` even though the v2 server never emits it.

Rationale: if the server adds a new `kind` (or starts populating
`context` / `prior_turns`) the client deserializes cleanly without
a refactor — the dataclass just has more populated fields. The
renderer ignores fields it doesn't care about. Future-proof
without speculation.

Rejected: trim to the v2-emitted subset (4 kinds, 5 fields). Smaller
surface, but every server-side addition becomes a client-side
refactor.

### D14: `done` payload's `message` field surfaces as `StreamEvent.text`

The server's `done` SSE payload uses `message` (see
`docs/002_api_reference.md` and `scufris_server/routes/chat_stream.py:376`);
v1's `done` payload used `text`. The SDK normalizes by reading
`message` from the wire and assigning to `StreamEvent.text`.

Rationale: the v1 CLI renderer at `feature/opencode:cli.py:185`
reads `ev.text` for the final reply. Renaming the SDK field to
`message` would force the CLI port to change every reference; the
SDK normalization is one line.

Rejected: rename `StreamEvent.text` → `StreamEvent.message` to
match the wire. Forces a renaming pass through every consumer
(CLI today, Telegram future) for cosmetic alignment.

### D15: `mypy --strict` + `ruff` extended to `scufris_client/` and `scufris_cli/`

Both new package directories are added to the implicit `mypy
--strict` and `ruff check / format` invocations. Tests
(`tests/unit/test_scufris_*.py`, `tests/integration/test_cli_real.py`)
stay on the default mypy mode (untouched — current convention is
strict on src only).

Rationale: matches the bar set by `scufris_server` (where
`--strict` has been clean since #10). The CLI and SDK are
first-party code that should hit the same bar.

Rejected: strict on SDK only (CLI uses `rich` which has incomplete
stubs). Negotiable — if `rich` types make strict prohibitive on
`scufris_cli/main.py`, drop strict there with an inline override
comment naming the rich issue. Decide during step 4 when the
actual friction is visible.

### D16: No new pyproject.toml dependencies

The CLI imports `rich`, `httpx`, stdlib (`asyncio`, `argparse`,
`getpass`, `readline`, `pathlib`, `os`, `logging`). All present
in current `pyproject.toml` deps. No new deps.

Rationale: keeps the wheel small; no dependency negotiation; no
manual pyproject edit beyond the trivial `packages` /
`only-include` / `scripts` updates at step 9.

Rejected: add `httpx-sse` for SSE parsing. Saves ~50 LoC of
hand-rolled parsing. Same call rejected on the server side (#11
D-implicit) — consistency wins; the parser is straightforward.

---

## Behavioural notes

- **Startup sequence:** parse args → setup logging → load config
  (server URL, full_thinking) → setup readline (with history
  file) → `async with ScufrisClient(...)` → `healthz()` probe
  (fail-fast on connection error with a "is the server running?"
  hint) → `resolve_identity()` (degrade per D9) → banner →
  REPL loop.
- **Banner format:** `Scufris CLI → <url> as <username|user N> [—
  linked surfaces: telegram, ...] — type /help for commands,
  Ctrl-D on empty line to exit.` Matches v1's format. If
  resolve degraded: `as user unknown`.
- **`/sessions` output:** one row per channel — `<channel_id> ·
  <surface>/<surface_id>/<agent> · last used <ISO ts>`. Sorted
  by `last_used_at DESC`. Empty list prints `no sessions yet`.
- **`/clear` confirmation:** `cleared N sessions` (where N is the
  count of session_links deleted). No interactive confirmation
  prompt — matches v1.
- **`/thinking` semantics:** `/thinking` with no arg prints
  current mode; `/thinking full` / `/thinking short` sets.
  `--short-thinking` argparse flag is start-time only; toggles
  via `/thinking` during the session win.
- **`/multiline` semantics:** toggle; when ON, hit a line
  containing only `.` to submit the buffered input. Matches v1
  exactly.
- **Ctrl-C during prompt:** prints blank line and continues
  (does NOT exit). Ctrl-C during a stream: cancels the stream
  (prints "interrupted — server canceled"). Ctrl-D on empty
  prompt: exits cleanly with "bye!".
- **History file:** `~/.scufris_cli_history`, 1000 entries,
  written on exit via `atexit`. Tolerant of missing file on
  first launch.
- **Renderer dimming:** thinking events render with `[grey50]`
  prefix and indentation `"  " * ev.depth` (always 0 in v2 but
  formatted defensively). Tool calls cyan, tool results green
  (red on failure), permission/meta dim grey, text deltas grey.
- **Final reply rendering:** `rich.Panel(rich.Markdown(text),
  title="scufris", border_style="green")`. Same as v1.
- **Server-down at launch:** `ScufrisConnectionError` from the
  `healthz()` probe → red message + hint about
  `SCUFRIS_SERVER_URL` → exit (non-zero). No retry loop.
- **Server-down mid-session:** the next chat call surfaces
  `ScufrisConnectionError`; we print the same red message but
  return to the prompt instead of exiting (matches v1).
- **Mid-stream `error` SSE event:** `_handle_message` prints the
  error in red and skips the Markdown panel. `error_type` is
  shown in dim grey for diagnostics.
- **No tools-list discovery:** the CLI doesn't enumerate
  available tools; that's opencode's domain. Tool calls show up
  in the trace as they happen.

---

## TOML schema impact

None. `config.toml` (#12) already has `[user.identity]` for
CLI-surface_id → user_id mapping (used server-side); the CLI
doesn't read TOML directly — it relies on env vars + resolve.

Optionally: add `[client]` section with `server_url` and
`full_thinking` keys (parity with v1's `[client]` section). Out
of scope for v0; deferred to a future task if config-driven
defaults become useful. Env vars cover the immediate need.

No new env vars beyond `SCUFRIS_USER`, `SCUFRIS_SERVER_URL`,
`SCUFRIS_FULL_THINKING` (all carried from v1).

---

## Module layout

Adds:

```
scufris_client/
├── __init__.py     # NEW — re-exports the public surface
└── client.py       # NEW — ScufrisClient + StreamEvent + ThinkingEvent
                    #       + errors + _parse_sse_stream (~450 LoC)

scufris_cli/
├── __init__.py     # NEW — empty marker
└── main.py         # NEW — REPL: render + dispatch + main loop (~400 LoC)
```

Touches:

```
docs/
├── 000_overview.md       # MOVE: CLI from "not yet" → "today"
├── 001_architecture.md   # ADD: client-packages section + map rows
└── 006_development.md    # UPDATE: layout, test counts, backlog

pyproject.toml            # USER EDIT: packages + only-include + scripts
cli.py                    # OPTIONAL DELETE: empty stub at repo root
```

Tests touched:

```
tests/unit/
├── test_scufris_client.py    # NEW — SDK respx coverage (~20 tests)
└── test_scufris_cli.py       # NEW — render + dispatch (~12 tests)

tests/integration/
└── test_cli_real.py          # NEW — SDK end-to-end (90s budget, 1 test)
```

---

## TODOs (in implementation order)

Each step is small enough to land in one commit and review
independently. User approves 1-2 steps at a time.

0. **[DONE] Pre-step doc-bug fix in `docs/002_api_reference.md`.**
   - [x] Replace `text_delta` / `reasoning` kind names with the
         real `text` / `tool_call` / `tool_result` / `tool_meta`
         set (mapper emits four; reasoning deltas are intentionally
         filtered).
   - [x] Add `tool_result` row to the kind table (was missing).
   - [x] Add `arg` row to the field table (only optional field v2
         actually populates).
   - [x] Fix wire excerpt: real kind names, `"source":"scufris"`
         (was `"opencode"`), realistic text-delta payload.
   - [x] Fix prose at lines 417 ("subagent spawns ... reasoning
         text"), 502 ("concatenate every text_delta"), 636
         ("usually a reasoning-delta").
   - [x] Add brief note explaining reasoning deltas are
         intentionally dropped and `compaction` kind is reserved-
         not-emitted in v2.
   - Filed as separate follow-up (NOT fixed here): the doc and
     server disagree on whether `type` is in the `thinking` and
     `error` SSE payloads — server omits, doc claims present. See
     §post-close-out follow-up.

1. **[DONE] Expand this TASK.md from 12-line stub.** [this file]
   - [x] Spec out the SDK surface (D1, D2).
   - [x] Encode D1-D16.
   - [x] 10-step implementation plan with gates per step.
   - [x] User approves; commit. (Approval implicit in "continue"
         message; commit deferred to bundled step 1+2 commit.)

2. **[DONE] SDK base: `scufris_client/__init__.py` + `client.py`
   (errors + class + non-streaming methods).**
   - [x] Create `scufris_client/__init__.py` re-exporting
         `ScufrisClient`, `ScufrisError`, `ScufrisConnectionError`,
         `ScufrisAuthError`, `ScufrisServerError`. (47 LoC.)
   - [x] Create `scufris_client/client.py` with:
         - Four exception classes (`ScufrisError` base extends
           `RuntimeError`).
         - `ScufrisClient` class with `__init__(base_url, *,
           timeout, transport)`, `__aenter__`, `__aexit__`,
           `aclose`, internal `_json(method, path, *, params,
           json)`, static `_raise_for_status`.
         - Methods: `healthz()`, `version()`,
           `resolve_identity(surface, surface_id)`.
         - Defensive checks: `resolve_identity` rejects replies
           missing `user_id: int`; `_json` rejects non-JSON
           and non-dict bodies. (~327 LoC after ruff format.)
   - [x] `tests/unit/test_scufris_client.py` — 18 test functions
         (25 cases after parametrize) using `httpx.MockTransport`
         (not respx; transport-injection is more explicit and
         matches the v1 SDK test pattern). Covers each method's
         happy path + error mapping (4 status-class branches +
         non-connect transport error + non-JSON body + non-dict
         body + error-field fallback) + `async with` lifecycle +
         env-var resolution + default-URL fallback + trailing-
         slash normalisation + exception hierarchy. ~227 LoC.
   - **Deviation logged for close-out**: TASK plan said ~10 tests
     with respx; actual is 25 cases with MockTransport. Stricter
     coverage, same intent.
   - Gates **PASS** (verified): `ruff check scufris_client tests/
     unit/test_scufris_client.py` clean; `ruff format --check`
     clean (1 file auto-reformatted); `mypy --strict
     scufris_server scufris_client` Success on 24 src files;
     `pytest tests/unit` → **317 passed in 4.49s** (was 292;
     +25 new).

3. **[DONE] SDK chat + state: `chat_stream()`, `sessions()`,
    `clear_session()`, `clear()`.**
    - [x] Add `StreamEvent` and `ThinkingEvent` dataclasses to
          `client.py`.
    - [x] Implement `_parse_sse_stream(response)` async-iterator
          helper. Drops comment lines. Yields `(event_name,
          payload_dict)`.
    - [x] Implement `chat_stream(surface, surface_id, agent,
          message)` — opens POST /v1/chat/stream, iterates the
          parser, normalizes each event to a `StreamEvent` (D12,
          D14).
    - [x] Implement `sessions(user_id)`, `clear_session(channel_id)`,
          `clear(user_id)` — plain JSON round-trips.
    - [x] Re-export `StreamEvent`, `ThinkingEvent` from
          `__init__.py`.
    - [x] Extend `test_scufris_client.py` — 14 more tests:
          chat_stream happy path (thinking → done), error path
          (thinking → error), comment-line drop, sessions empty
          + populated + user_id query param + non-list rejection,
          clear_session cleared-true/false, clear count + zero-count
          + request-body-shape, SSE parse multiline data + trailing
          event no final blank + unknown fields dropped, malformed
          JSON, missing required field. 45 tests total.
    - Gates **PASS** (verified): `ruff check` clean; `ruff format`
      clean (3 files already formatted); `mypy --strict` Success on
      2 source files; `pytest tests/unit` → **45 passed**.

4. **CLI plumbing: `scufris_cli/__init__.py` + `main.py`
    skeleton (argparse + readline + identity + REPL loop).**
    - [x] Create `scufris_cli/__init__.py` (with module comment).
    - [x] Create `scufris_cli/__main__.py` with:
          - Constants: `HISTORY_FILE`, `HELP_TEXT`,
            `THINKING_SHORT_LIMIT`, `_AGENT`.
          - Helpers: `_truncate`, `_display_name`, `_is_sub_agent`,
            `_Settings` dataclass.
          - `_setup_readline()`, `_save_readline_history()`,
            `_read_input(console, multiline)`.
          - `make_render_thinking(console, settings)` — 4 branches
            (tool_call, tool_result, tool_meta, text), no compaction,
            no prior_turns/context, no is_sub_agent verb split.
          - `_handle_message(console, client, surface_id, message,
            render_thinking, logger)` — drives chat_stream, renders
            thinking events live, panel-prints final reply, handles
            all 3 error types + KeyboardInterrupt.
          - `_handle_command(console, client, user_id, surface_id,
            cmd, multiline, settings) -> (should_exit, new_multiline)`
            — slash dispatch for all six commands (D4).
          - `_amain(args)` — sets up Console, loads env vars,
            opens `ScufrisClient`, runs `healthz()` + identity
            resolve, prints banner, enters main REPL loop
            (input read → /command dispatch or chat_stream → render).
          - `main()` entry with argparse (`--short-thinking`,
            `-q`/`--quiet`).
    - [x] Tests deferred — covered by tests at step 6.
    - Gates: ruff clean on `scufris_cli/` ✓; `ruff format` auto-reformatted 1 file;
      mypy --strict clean ✓ (removed unused `type: ignore`; D15 strict
      fully passes — no rich friction observed).

5. **CLI renderer: `make_render_thinking()` + plug into REPL.**
   - [ ] Add `make_render_thinking(console, settings)` to
         `main.py` per D5: four branches (`tool_call`,
         `tool_result`, `tool_meta`, `text`), no `compaction`,
         no `prior_turns`/`context` conditionals, no
         `is_sub_agent` split.
   - [ ] Wire `render_thinking` into the REPL: replace the
         plain-string event printer (step 4) with the
         renderer's `render_thinking(ev.thinking)` call when
         `ev.kind == "thinking"`.
   - [ ] Add `_handle_message(...)` per the v1 port:
         try/except for all 3 error types + KeyboardInterrupt,
         final reply via `Panel(Markdown(...))`.
   - [ ] No tests yet — covered alongside slash dispatch at
         step 6.
   - Gates: ruff clean, mypy --strict clean.

6. **CLI slash dispatch + `/sessions` command.**
   - [ ] Add `_handle_command(...)` with the six commands per
         D4. `/sessions` calls `client.sessions(user_id)` and
         renders one row per channel per the behavioural note
         format.
   - [ ] Wire into main loop: `if stripped.startswith("/"):
         _handle_command(...)`.
   - [ ] `tests/unit/test_scufris_cli.py` — ~12 tests:
         renderer branch per kind (4), `_handle_command`
         dispatch (6), `_read_input` multiline,
         `make_render_thinking` settings.full_thinking respect.
         Mock the `ScufrisClient` via dataclass-stub.
   - Gates: ruff clean, mypy --strict clean, all tests pass.

7. **Integration test: `tests/integration/test_cli_real.py`
   (90s budget).**
   - [ ] Spawn `scufris-server` subprocess on a free port (use
         `subprocess.Popen` with a port-finding helper); wait
         for `/v1/healthz` to return 200.
   - [ ] Open a real `ScufrisClient` against that server.
   - [ ] Resolve identity for `surface="cli"`,
         `surface_id="integration"`.
   - [ ] Run `chat_stream(..., message="reply with 4")` to
         completion; assert at least one `thinking` event, a
         `done` event with `text` containing "4",
         `oc_session_id.startswith("ses_")`.
   - [ ] Cleanup: kill the spawned server; clear the channel.
   - [ ] Mirrors `tests/integration/test_chat_stream_real.py`
         setup; reuses the `opencode_url`, `ollama_default_model`
         fixtures.
   - Gates: ruff clean, mypy --strict clean, integration test
     passes within 90s.

8. **Docs refresh.**
   - [ ] `docs/000_overview.md`: move CLI from "not yet" →
         "today"; refresh caveat date.
   - [ ] `docs/001_architecture.md`: new short "Client
         packages" subsection (location of SDK and REPL, who
         depends on whom); module map gains two rows.
   - [ ] `docs/006_development.md`: layout block adds both
         packages + test files; unit test count + ~32;
         integration list adds `test_cli_real.py` with 90s
         ceiling; backlog table marks #14 closed.
   - [ ] No `docs/002_api_reference.md` changes (step 0 fix
         already in; CLI isn't a wire-format change).
   - Gates: ruff clean (no code touched).

9. **pyproject + close-out.**
   - [ ] **User edits `pyproject.toml`:**
         - `[project.scripts] scufris-cli = "scufris_cli.main:main"`
           (was `"cli:main"`).
         - `[tool.hatch.build.targets.wheel] packages` adds
           `scufris_client`, `scufris_cli`.
         - `only-include` adds `scufris_client`, `scufris_cli`;
           optionally remove `cli.py`.
   - [ ] **Optionally delete `cli.py`** at repo root (empty stub).
   - [ ] Full test suite: unit pass, integration pass, ruff
         clean, mypy --strict clean for `scufris_server`,
         `scufris_client`, `scufris_cli`.
   - [ ] Live re-run: `uv run --active scufris-cli` against
         live server pid=29482 (or freshly spawned); send one
         turn, see thinking events, exit cleanly.
   - [ ] `tatr new` for the `type` field asymmetry follow-up
         (server omits `type` from `thinking` and `error` SSE
         payloads but doc claims it; decide whether to add
         server-side for symmetry or just document the
         asymmetry).
   - [ ] Mark `tasks/20260613-091049/TASK.md` `STATUS: CLOSED`.
   - [ ] Add closing notes summarising LoC added, test counts,
         decisions taken during impl.

---

## Acceptance criteria

- [ ] `uv run scufris-cli` (after pyproject repoint at step 9)
      drops into a prompt, prints banner with resolved username.
- [ ] One-turn chat works end-to-end: user types a message,
      thinking events render live, final reply renders as a
      Markdown panel.
- [ ] All six slash commands work per D4 (`/help`, `/clear`,
      `/sessions`, `/thinking`, `/multiline`, `/exit`/`/quit`).
- [ ] `/sessions` lists every channel for the resolved user
      with channel id, surface, surface_id, agent, last-used
      time.
- [ ] `/clear` deletes every channel for the resolved user
      (D11) and reports the count.
- [ ] `/thinking full|short` toggles renderer mode; runtime
      change visible on the next turn.
- [ ] `/multiline` toggles; multi-line input submitted with
      `.` on its own line.
- [ ] Ctrl-D on empty prompt exits cleanly; Ctrl-C on prompt
      blanks the line and continues; Ctrl-C during stream
      cancels.
- [ ] Server-down at launch → red "server unreachable" + hint
      + non-zero exit. Server-down mid-session → red message
      + return to prompt.
- [ ] Identity-resolve failure → degraded banner ("user
      unknown") + REPL still chats (D9).
- [ ] Pre-stream `503` from /v1/chat/stream surfaces as
      `ScufrisServerError` with the server's structured body.
- [ ] Mid-stream `error` SSE event surfaces in red with
      `error_type` shown.
- [ ] SDK covers the seven endpoints listed in D2.
- [ ] `tests/unit/test_scufris_client.py` ~20 tests pass.
- [ ] `tests/unit/test_scufris_cli.py` ~12 tests pass.
- [ ] `tests/integration/test_cli_real.py` 1 test passes
      (real server + opencode + ollama) within 90s.
- [ ] Existing 292 unit + 4 integration tests still pass after
      additions.
- [ ] `mypy --strict` clean on `scufris_server`,
      `scufris_client`, `scufris_cli` (CLI scope possibly
      reduced per D15 fallback).
- [ ] `ruff check` + `ruff format --check` clean on every
      touched file.
- [ ] No new pyproject.toml dependencies (D16).
- [ ] `tatr new` follow-up filed for the SSE-payload `type`
      asymmetry uncovered in step 0.
- [ ] TASK.md marked `STATUS: CLOSED` with closing notes.

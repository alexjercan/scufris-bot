# Add post-clear coverage to chat-route + integration tests

- STATUS: OPEN
- PRIORITY: 40
- TAGS: test-gap,sessions,bugfix

## Context

On `feature/opencode-v2` during #11 step 12 (curl probe), alex hit
HTTP 500 with `sqlite3.IntegrityError: UNIQUE constraint failed:
channels.user_id, channels.surface, channels.surface_id,
channels.agent` on `POST /v1/chat/stream`. Same bug applies to
`POST /v1/chat`. Pre-existing since #10 (sessions split).

Root cause: `routes.chat._resolve_session` returns `None` for two
distinguishable states ("no channel" vs "channel exists, no
session_link — i.e. orphan"). Both routes unconditionally called
`sessions.create_channel_link` on `None`, and that function
INSERTed `channels` without checking for an existing row. After
any `/v1/clear` or `/v1/sessions/{id}/clear` (per ADR-13 — clears
preserve `channels`, drop only `session_links`), the next chat on
the same tuple crashed.

**Fix landed** in `sessions.create_channel_link`
(`scufris_server/sessions.py:175`) — lookup-first pass, reuses the
existing `channels.id` when present, INSERTs only the missing
`session_links` row. Unit test
`test_create_channel_link_relinks_orphan_channel_after_clear`
(`tests/unit/test_sessions.py:153`) covers the service layer.

## Why this task exists

The route layer and the integration suite never exercised the
relink path:

- `tests/unit/test_chat_route.py` — 18 tests; none clear then
  re-chat the same channel.
- `tests/unit/test_chat_stream_route.py` — 13 tests; same gap.
- `tests/integration/test_chat_real.py` / `test_sessions_real.py`
  — happy paths and clear-idempotency, but no clear-then-rechat.

Without route-level coverage, a future refactor to `_resolve_session`
or to the chat handler's call into `create_channel_link` could
silently regress the fix and the unit test in `test_sessions.py`
wouldn't catch it (it tests the service in isolation).

## Tasks

- [ ] `tests/unit/test_chat_route.py`: add
      `test_chat_relinks_orphan_channel_after_clear` —
      `POST /v1/chat` once, clear via `clear_channel_link`
      directly, `POST /v1/chat` again with the same channel,
      assert 200 + the new `oc_session_id` differs from the
      cleared one + DB has exactly one `channels` row + one
      `session_links` row for the tuple.
- [ ] `tests/unit/test_chat_stream_route.py`: same shape but
      via `_read_stream_body` and the FakeEventBus pattern;
      asserts on the `done` payload's `oc_session_id`.
- [ ] `tests/integration/test_chat_real.py`: add a third subtest
      (or new test) that exercises the live clear-then-rechat
      flow against real opencode + ollama. Two opencode sessions
      should be created; the second should differ from the first.
      Stays under the 90s budget by reusing the existing
      `opencode_url` / `ollama_default_model` fixtures.
- [ ] Document the relink semantic in `docs/002_api_reference.md`
      under `/v1/chat` and `/v1/chat/stream` ("After a clear, the
      next chat on the same channel allocates a new opencode
      session; the channel id is preserved").

## Gates

- ruff check + format clean on touched files.
- mypy --strict scufris_server clean.
- All unit tests pass (~295 expected after this task).
- Integration suite passes against live opencode (`uv run
  --active pytest -m integration tests/integration/` with
  `OPENCODE_URL` set).

## Out of scope

- Refactoring `_resolve_session` to return `(channel_id,
  oc_session_id | None)` — the current shape is fine; the
  service-layer fix absorbs the orphan case transparently.
- Adding a "channel last used" pruner — that's #34
  (`20260616-111430`).

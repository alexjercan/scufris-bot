# Telegram bot v2: migrate to scufris_client

- STATUS: CLOSED
- PRIORITY: 100
- TAGS: telegram,bot,scufris-client

## Summary

`bot.py` is the Telegram front-end for Scufris. It currently talks to
`scufris-server` through an older client shape (positional
`user_id` + message on `chat_stream`, a `client.stats(user_id)` call,
etc.). The new `scufris_client.ScufrisClient` (v2 SDK) has landed
with a different surface — channel-based (`surface` /
`surface_id` / `agent`) rather than resolved-`user_id`-based for
streaming, and it does not expose a `stats` endpoint at all.

This task is to port `bot.py` onto the new `scufris_client` while
preserving the bot's existing UX (typing indicator, live-edited
"thinking" placeholder, collapsible thinking trace, slash commands).
This is a client swap, not a UX rewrite — no new features.

Equivalent to v1 task `20260513-121619`, retargeted at the v2 server.
Spun off from the scufris-v2 planning doc
(`tasks/20260613-085701/TASK.md`).

## Background / why this matters

The bot was written against an earlier client interface. Diffing
`bot.py` against the current `scufris_client/client.py` surfaces three
concrete breaks, not just a "swap the import" job:

1. **`chat_stream` signature changed.**
   Bot calls `client.chat_stream(user_id, user_message)`. The new SDK
   is `chat_stream(surface, surface_id, agent, message)` and returns
   `StreamEvent` (kinds: `thinking` / `done` / `error`), not the old
   `ev.kind == "thinking" / "done" / "error"` shape directly on a
   flat event — thinking events are now nested under
   `StreamEvent.thinking`. The bot's `async for ev in
   client.chat_stream(...)` loop and its `ev.thinking`, `ev.text`,
   `ev.error` access need to be reconciled with this.
   - Also: `agent` is now a required parameter (e.g. `"build"`,
     matching the CLI's hardcoded choice per v2 design decision D3).
     Need to decide what the bot passes — likely a fixed `"build"` to
     match CLI parity, unless there's a reason to expose agent
     switching later (out of scope here, but worth a one-line note in
     the code).

2. **No more separate `resolve_identity` → raw integer `user_id`
   passed into `chat_stream`.** The new `chat_stream` takes
   `surface` + `surface_id` directly — it's unclear from the client
   alone whether `resolve_identity` is still needed at all for this
   call path, or only for `/clear` and `sessions`, which still take
   an integer `user_id`. This needs to be resolved during
   implementation (see open questions).

3. **`client.stats(user_id)` does not exist on the new SDK.** The
   v2 `ScufrisClient` has `healthz`, `version`, `resolve_identity`,
   `chat_stream`, `sessions`, `clear_session`, `clear` — no `stats`.
   `/stats` is a real, used command (`format_telegram_stats`,
   per-agent table, tool histogram). Either:
   - the server needs a `/v1/stats` endpoint + a corresponding SDK
     method, or
   - `/stats` is descoped from this task and tracked separately.
   This needs a decision before implementation (see open questions) —
   it is not safe to just silently drop the command.

## Scope

In scope:
- Update `bot.py`'s `chat` handler to call the new `chat_stream`
  signature and unpack `StreamEvent` correctly.
- Update `clear_history` to use the new `clear(user_id)` (this one
  looks compatible as-is — verify field names on the response, e.g.
  `cleared` vs `count`, since the client docstring says the wire
  field is `count` but `bot.py` reads `result.get("cleared", 0)`).
- Decide and implement the fate of `/stats` (see open questions).
- Keep `_resolve_user_id` / identity caching if still needed for
  `/clear` and `/stats`; remove it if `chat_stream` no longer needs a
  resolved id and nothing else does either.
- Preserve existing UX: typing action, placeholder message with live
  edits via `PlaceholderRenderer`, collapsible "Show thinking" toggle
  and its cache, rate limiting of placeholder edits, message-length
  truncation/chunking behavior.
- Update exception handling in `chat`/`clear_history`/`stats_command`
  to match whatever the final `ScufrisError` subclass hierarchy looks
  like (it appears unchanged — `ScufrisConnectionError`,
  `ScufrisAuthError`, `ScufrisServerError` — confirm no drift).
- Update `_post_init`'s health probe if `healthz()`'s response shape
  changed (looks the same; verify).

Out of scope:
- Restructuring `bot.py` into a package (`scufris_bot/__main__.py`
  etc.) — explicitly deferred, noted as a follow-up, not part of this
  task.
- Any new bot features (agent switching, new slash commands).
- Server-side work to add a `/v1/stats` endpoint, unless the open
  question below resolves to "yes, add it" — if so, split that into
  its own task and only consume it here.

## Open questions (resolve before/at start of implementation)

1. Does `chat_stream` still need a resolved `user_id`, or does
   `surface` + `surface_id` (the raw Telegram id) suffice on its own
   now? If identity resolution is now folded into the server's
   channel lookup, `_resolve_user_id` may only be needed for
   `/clear` and `/stats` going forward. A: the `surface, surface_id, agent`
   triple get's resolved to an user id.
2. What happens to `/stats`? Add server endpoint + SDK method, or
   drop/stub the command for now with a "temporarily unavailable"
   message and a follow-up task? A: We drop the command
3. What `agent` value does the bot pass to `chat_stream`? Assume
   `"build"` (CLI parity) unless told otherwise. A: `build`
4. Confirm the `clear()` response field name (`count` per client
   docstring vs `cleared` per `bot.py` today) and update
   `clear_history`'s formatting accordingly. A: we should use `clear()` for the
   `/clear` command.

## Acceptance criteria

- [x] `scufris_bot/__main__.py` imports and calls only methods that exist on
  the current `scufris_client.ScufrisClient` — no calls to methods that aren't
  part of the SDK's public surface.
- [x] `/start`-equivalent plain chat messages stream correctly:
      typing indicator fires, placeholder message appears and is
      live-edited as `thinking` events arrive, final answer is sent
      as a new message with the "Show thinking ▼" toggle attached.
- [x] Tapping "Show thinking" / "Hide thinking" still works against
      the new event data (trace text reconstruction unaffected by the
      `chat_stream` signature change).
- [x] `/clear` correctly reports the number of cleared
      messages/sessions using the new client's actual response shape
      (field name verified, not assumed).
- [x] `/stats` either works end-to-end against a real server endpoint,
      or is cleanly stubbed with a clear "not available yet" message
      — not left calling a nonexistent method that throws an
      `AttributeError` at runtime.
- [x] All three existing error paths (`ScufrisConnectionError`,
      `ScufrisAuthError`, `ScufrisServerError`) are still caught in
      `chat`, `clear_history`, and `stats_command` and produce the
      same user-facing error messages as before (modulo `/stats`
      depending on Q2's resolution).
- [x] `_post_init`'s server reachability probe (`healthz`) still
      gates bot startup the same way (`SystemExit(1)` on
      unreachable/auth-failed/error).
- [x] No regression in placeholder edit rate-limiting or message
      chunking/truncation behavior (`PLACEHOLDER_EDIT_INTERVAL`,
      `PLACEHOLDER_MAX_LEN`, `TELEGRAM_MAX_MESSAGE` logic untouched
      unless the new event shape forces a change).
- [x] Manual smoke test: send a message, confirm streamed thinking
      updates appear, confirm final answer + toggle, confirm
      `/clear` and `/stats` (or its stub) behave as expected.
- [x] No leftover references to old client method signatures or
      removed exception types.

## Notes for implementer

- Treat `PlaceholderRenderer`, `_thinking_cache`,
  `format_telegram_stats`, and the keyboard helpers as UX logic that
  should not need to change — only the data feeding into them
  (`StreamEvent` / `ThinkingEvent` shapes) should.
- Don't fold the package restructuring into this diff — keep the
  client migration reviewable on its own.

# Telegram bot: port main.py to use scufris-server HTTP client

- STATUS: OPEN
- PRIORITY: 90
- TAGS: feature,telegram,refactor

## Goal

Replace the in-process agent invocation in `main.py` with a thin
adapter that talks to `scufris-server` over HTTP, mirroring how
`scufris-cli` already works. After this change the Telegram bot is one
of two clients of the same daemon — same agents, same memory, same
runtime — instead of running its own duplicate copy of the agent stack.

## Scope

### In
- Strip agent construction (`setup_scufris`, `create_agent_manager`,
  `ChatHistoryManager`, `ToolCallbackHandler`) out of `main.py`.
- Use `scufris_client.ScufrisClient` to send the user's message and
  stream the response.
- Map Telegram `user_id` → server `user_id` so per-user history is
  preserved across both Telegram and CLI sessions.
- Keep telemetry and logging behaviour: each Telegram message still
  calls `begin_turn(f"telegram:{user_id}")`.
- Forward streaming "thinking" / tool events to Telegram as edits to
  a placeholder message (Telegram doesn't do SSE; reuse the
  existing edit-in-place pattern from the current bot).
- Configurable `SCUFRIS_SERVER_URL` and `SCUFRIS_TOKEN` (env, with
  sensible defaults pointing at `127.0.0.1:8765`).
- Update the `scufris-bot` console script docstring + README to make
  the dependency on a running `scufris-server` explicit.
- Update or add tests: bot tests should stub `ScufrisClient`, not the
  agent manager.

### Out
- New Telegram features (commands, voice, etc.).
- Changes to the server API.
- Multi-bot / multi-tenant routing.

## Acceptance criteria

- `main.py` no longer imports `setup_scufris`, `create_agent_manager`,
  or any in-process agent helpers.
- Sending a Telegram message produces the same response as
  `scufris-cli` would, sharing the same `(user_id, agent)` history
  slices on the server.
- `pytest`, `ruff`, `mypy`, and `nix flake check` stay green.
- `scufris-bot` exits with a clear error if the server is unreachable
  on startup (no silent partial bring-up).
- README documents the new "start the server first, then the bot"
  flow.

## Notes

- This is the largest single piece of carried-over scope from the May
  sprint; everything else assumes a one-process-one-job split.
- `cli_embedded.py` was deleted last sprint — keep that property: there
  is exactly one in-process agent path and it lives in the server.
- Consider whether the bot needs its own bearer token or can share the
  CLI's `SCUFRIS_TOKEN`. Probably share for v1; document.

## References

- `tasks/20260510-192636/TASK.md` — the equivalent CLI port (CLOSED).
- `tasks/20260510-192505/TASK.md` — server endpoints + auth contract.
- `scufris_client/client.py` — already-built reusable client.

# CLI as HTTP client of scufris-server

- STATUS: OPEN
- PRIORITY: 75
- TAGS: deploy,cli

## Goal

Refactor `cli.py` so that it is a thin HTTP client of the `scufris-server`
daemon (see task 20260510-192505). The interactive REPL, slash commands, and
streaming "thinking" UX must remain visually identical to today, but all agent
work happens in the daemon.

This decouples the CLI lifecycle from the agent's: starting/stopping the
terminal no longer evicts history, and multiple clients (CLI, Telegram,
future TUI) can share one running brain.

## Scope

### In
- New module `scufris_client/` (or `utils/client.py`) implementing:
  - `ScufrisClient(base_url, token=None)` with `.chat()`, `.chat_stream()`,
    `.stats()`, `.clear()`, `.healthz()`.
  - SSE parser yielding `ThinkingEvent`-shaped dataclasses (reuse the
    existing `ThinkingEvent` type from `utils/callbacks.py` so `render_thinking`
    is unchanged).
- Rewrite `cli.py`:
  - Bootstrap no longer instantiates `AgentManager`/`ChatHistoryManager`.
  - `process_user_input` becomes `await client.chat_stream(user_id, text)`,
    feeding events into the existing `render_thinking` dispatcher.
  - Slash commands map 1:1 to client methods:
    - `/stats` → `GET /v1/stats`
    - `/clear` → `POST /v1/clear`
    - `/quit`, `/help` stay local.
  - Connection settings from env: `SCUFRIS_SERVER_URL`
    (default `http://127.0.0.1:8765`), `SCUFRIS_TOKEN`.
  - User identity: `SCUFRIS_USER` env, fallback to `getpass.getuser()`.
- Friendly errors:
  - Connection refused → "scufris-server not reachable at <url>" + hint.
  - 401 → "bad/missing SCUFRIS_TOKEN".
  - SSE disconnect mid-stream → cancel render gracefully, return to prompt.
- Tests in `tests/test_cli_client.py` using a fake aiohttp/httpx server (or
  `respx`) covering: happy-path stream, stats, clear, auth failure, server-down.

### Out
- Embedded fallback (running the agent in-process when no server is
  reachable). Tracked as a possible follow-up; v1 requires a server.
- Telegram bot migration (separate task, not yet created).
- Daemon auto-spawn from CLI.

## Acceptance criteria

- `scufris-cli` (new console script entry, see Nix task) connects to
  `$SCUFRIS_SERVER_URL` and works exactly like today's REPL: prompt,
  thinking trace, tool calls, final answer, slash commands.
- Killing and restarting the CLI preserves conversation history (proves
  state lives in the daemon).
- Two CLI instances using the same `SCUFRIS_USER` see the same history;
  different users are isolated.
- Ctrl-C during streaming cancels the in-flight request server-side
  (client closes the SSE connection; server cancels the task).
- `pytest tests/test_cli_client.py` green; no real network calls in tests.

## Notes

- HTTP lib: prefer `httpx` (async, SSE via `client.stream("GET", ...)`),
  already a transitive dep of many things; confirm in pyproject.
- SSE parser is small enough to write by hand (parse `event:` and `data:`
  lines, dispatch JSON to `ThinkingEvent.from_dict`-style constructor).
  Avoid pulling in `sseclient-py` (sync only) or `httpx-sse` if a 30-line
  parser suffices.
- `render_thinking` in `cli.py` is the contract: keep `ThinkingEvent`'s
  field set stable across server/client to avoid a serialization layer.
- Old in-process bootstrap can move to `cli_embedded.py` for emergencies
  and dev, but not advertised.

## References

- `cli.py` (current REPL: bootstrap, process_user_input, render_thinking,
  slash commands).
- `utils/callbacks.py` — `ThinkingEvent` dataclass (the SSE payload shape).
- `tasks/20260510-192505/TASK.md` — server task this depends on.
- `tasks/20260510-192350/TASK.md` — design spike that pins endpoint shapes.

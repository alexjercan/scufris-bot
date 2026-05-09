# Phase 3.4 — /clear wipes all per-agent slices; /stats per-agent breakdown

- STATUS: CLOSED
- PRIORITY: 70
- TAGS: phase3,cli,telegram

> Implements decision **D5** of the
> [Phase 3 design doc](../20260509-165646/TASK.md). User-facing
> polish: make `/clear` actually clear *everything* and make `/stats`
> visible into the new per-agent slices.

## Scope

`/clear` already calls `history_manager.clear_history(user_id)`,
which Phase 3.1 made an alias for `clear_user(user_id)` — so
clearing already works correctly the moment 3.1 lands. **The work
here is the user-visible reporting**: tell the user what was
actually wiped, and let `/stats` show the new per-agent breakdown.

### CLI (`cli.py`)

1. **`/clear` handler.** Use the return value of `clear_user`
   (total messages removed) and break it down by querying
   `get_stats()` *before* clearing:
   ```
   Cleared 14 messages (scufris: 8, knowledge_agent: 4, journal_agent: 2).
   ```

2. **`/stats` handler.** Render the new `messages_per_agent` block:
   ```
   Total users: 1
   Total messages: 14
     scufris: 8
     knowledge_agent: 4
     journal_agent: 2
   ```

### Telegram (`main.py`)

3. **`clear_history` handler.** Same wording change — show the
   per-agent breakdown of what was cleared.

4. **`stats` handler.** Same per-agent block in the response message.

## Out of scope

- A `/clear <agent>` sub-command — explicitly rejected in the master
  design doc (Decisions §3).
- Telemetry / observability beyond the existing `/stats`.

## Acceptance criteria

- [x] CLI `/clear` shows per-agent breakdown of what was wiped when
      sub-agent slices exist; falls back gracefully ("Cleared 8
      messages.") when only the scufris slice exists. *(Verified: empty
      → "no messages to clear"; mixed → "cleared 5 messages (scufris: 2,
      knowledge_agent: 2, journal_agent: 1)".)*
- [x] CLI `/stats` lists per-agent message counts. *(New handler in
      `cli.py`; registered in HELP_TEXT.)*
- [x] Telegram `/clear` and `/stats` mirror the CLI output (modulo
      Telegram-style markdown). *(`stats_command` added + registered
      via `CommandHandler("stats", stats_command)`.)*
- [x] After `/clear`, `get_stats()` shows zero messages for that user
      across all agents. *(Verified: `total_users: 0, total_messages: 0,
      messages_per_agent: {}`.)*

## Implementation notes

- Added `ChatHistoryManager.get_user_breakdown(user_id) -> Dict[str, int]`
  to avoid leaking the raw `_histories` dict to handlers.
- `/history` left unchanged here — its deprecation is **Phase 3.4b**
  (`tasks/20260509-172715`).
- HELP_TEXT updated to list `/stats`.

## Estimated effort

~30 minutes. Pure UI text + a couple of dict lookups.

## Dependencies

Hard-blocks-on **3.1** (needs `clear_user` and `messages_per_agent`).
Soft-blocks-on **3.3** (per-agent slices need to exist for the
per-agent reporting to be observable, but this task can ship even if
they're always empty).

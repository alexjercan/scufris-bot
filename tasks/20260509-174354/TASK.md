# Phase 3.4c — Remove deprecated `/history` command

- STATUS: DEFERRED
- PRIORITY: 30
- TAGS: phase3,cli,telegram,cleanup

> Cleanup task. **Phase 3.4b** (`tasks/20260509-172715`) added a
> deprecation notice to `/history` and stood up `/stats` as the
> replacement. This task removes `/history` entirely once enough
> dogfooding time has passed.

## Trigger

Reopen this task when:

- The user has been running on `/stats` exclusively for at least
  one work session, and
- No remaining muscle-memory uses of `/history` are reported.

## Scope

- `cli.py` — drop the `/history` branch from `_handle_command`;
  remove `/history` from `HELP_TEXT`.
- `main.py` — drop the `history_stats` handler and its
  `CommandHandler("history", ...)` registration. Telegram will then
  reply with the standard "unknown command" silence.
- Optional: scan the codebase one more time for stray `/history`
  references in docs, comments, or tatr task files.

## Acceptance criteria

- [ ] `/history` is not registered anywhere; typing it in the CLI
      yields the same "unknown command" path as e.g. `/foo`.
- [ ] `HELP_TEXT` no longer mentions `/history`.
- [ ] No regressions on `/stats` (existing 3.4 + 3.4b behaviour
      preserved).

## Estimated effort

~10 minutes. Pure deletion + a help-text edit.

## Dependencies

- Hard-blocks-on **Phase 3.4b** (`tasks/20260509-172715`).

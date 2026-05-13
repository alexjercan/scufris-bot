# Journal agent: natural-language reminders and date parsing

- STATUS: OPEN
- PRIORITY: 45
- TAGS: agents,journal,nlp

## Goal

Let the user say "remind me to call mum on Sunday at 6", "every
weekday at 9 take vitamins", or "in 20 minutes check the oven", and
have the journal agent parse the phrase, schedule a reminder, and
deliver it via the active surface (CLI banner / Telegram message).
Bridges the gap between the journal's task list and the calendar
integration.

## Scope

### In
- Add `utils/nl_dates.py`: a thin wrapper around `dateparser` (or
  `pendulum` + custom rules) that returns
  `{when: datetime, recurrence: RRULE | None, confidence: float}`.
- Add `reminder_create_tool`, `reminder_list_tool`,
  `reminder_cancel_tool`. Storage: SQLite table
  `reminders(id, user_id, text, when_utc, rrule, status, surface)`.
- Background dispatcher in `scufris-server`: every 30s, scan due
  reminders and deliver via the appropriate `surface` adapter
  (CLI / Telegram / future).
- Update `JOURNAL_AGENT_PROMPT` with reminder examples and an
  explicit "always confirm parsed time before creating" rule for
  ambiguous inputs (confidence < 0.8).
- If calendar integration (`20260513-121628`) is merged first,
  one-shot reminders also create a calendar event when the user
  says "schedule" or "put on my calendar".
- Tests: parse table of 30+ phrases against expected datetimes
  (frozen `now`); dispatcher integration test that fires within 60s
  of due time.

### Out
- Cross-device push notifications (handled by surface adapters,
  out of scope here).
- Snoozing UI / "remind me again in 10 min" — separate task once
  the basic flow lands.
- Multi-language NL parsing — English only for v1.

## Acceptance criteria
- 90%+ pass rate on the parse-table tests.
- `reminder_create_tool("call mum Sunday 6pm")` returns a row with
  the correct UTC `when` and `rrule=None`.
- `reminder_create_tool("every weekday 9am vitamins")` produces an
  RRULE that re-fires on the next weekday after each delivery.
- Restarting `scufris-server` does not lose pending reminders.
- Low-confidence parses bounce back to the user with
  "did you mean Sunday Jun 7 18:00?" before scheduling.

## Notes
- Reuse the journal agent's user_id / surface routing — don't invent
  a new identity layer.
- Dispatcher should be a single asyncio task in the server, not a
  separate process; document the at-most-once-ish delivery semantics
  in the README.

## References
- `utils/agent_builder.py:679` — `create_journal_agent`.
- `tasks/20260513-121628/TASK.md` — calendar integration (related;
  may share `when` parsing).
- `tasks/20260408-140414/TASK.md` — original journal-agent design.

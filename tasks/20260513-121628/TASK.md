# Journal agent: CalDAV/ICS calendar integration

- STATUS: OPEN
- PRIORITY: 50
- TAGS: agents,journal,calendar

## Goal

The journal agent owns daily structure (tasks, habits, notes) but has
no view of the user's actual calendar. Wire in CalDAV (read+write)
and ICS (read-only feeds) so the daily summary can include today's
events and so the agent can create events on request
("schedule a dentist appointment for next Tuesday at 10").

## Scope

### In
- Add `utils/tools/calendar_tools.py` with:
  - `calendar_list_today() -> [Event]`
  - `calendar_list_range(start, end) -> [Event]`
  - `calendar_create(title, start, end, location?, description?) -> Event`
  - `calendar_update(event_id, **fields) -> Event`
  - `calendar_delete(event_id) -> None`
- CalDAV backend via `caldav` library; ICS-feed backend via
  `icalendar` (read-only).
- Config schema additions in `config.py`:
  - `calendar.caldav.url`, `username`, `password_secret_ref`
    (sops-nix path),
  - `calendar.ics_feeds: list[{name, url}]`.
- Register all five tools on `journal_agent`; update
  `JOURNAL_AGENT_PROMPT` with usage examples and the rule that ICS
  feeds are read-only.
- Daily-view integration: when `daily_view_tool` runs, prepend
  today's events from all configured sources.
- Timezone handling: store/return UTC, render in the user's
  configured tz (`config.user.tz`).

### Out
- Google Calendar OAuth — CalDAV proxy works for Gcal too; revisit
  if users complain.
- Recurring-event editing UI complexity (single-instance edits
  on recurring series). v1: refuse with a clear message.
- Free/busy queries across multiple calendars.

## Acceptance criteria
- With a configured CalDAV server, `calendar_list_today()` returns
  events created via the official client and vice versa.
- Asking the agent "what's on today?" lists CalDAV + every ICS feed,
  deduped by `(title, start)`.
- Asking "schedule X tomorrow at 3pm for 1h" creates the event and
  returns its ID.
- Secrets are read via the existing sops-nix flow (see open carryover
  task `20260510-192923`); no plaintext passwords in `config.toml`.
- Tests use a mock CalDAV server (`radicale` in a fixture).

## Notes
- ICS feed polling should cache for ≥5 min to avoid hammering remote
  servers.
- Coordinate with NL-reminders task (`20260513-121629`) — that task
  will likely call `calendar_create` under the hood.

## References
- `utils/agent_builder.py:679` — `create_journal_agent`.
- `utils/tools/journal_tools.py` — pattern to follow for new tools.
- `tasks/20260408-140414/TASK.md` — original journal-agent design.
- `tasks/20260414-125122/TASK.md` — journal tooling expansion.
- `tasks/20260510-192923/TASK.md` — sops-nix secrets carryover
  (blocks production rollout, not local dev).

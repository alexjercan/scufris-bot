# Unit tests — utils/stats.py (formatters + table render)

- STATUS: OPEN
- PRIORITY: 16
- TAGS: testing,quality

Lock down the `/stats` rendering so future schema additions (extra
columns, new agents) don't silently break alignment.

## Scope

`tests/test_stats.py`:

- `format_relative`
  - `None` → `"—"`.
  - Future timestamps → `"just now"`.
  - <60s, <60m, <24h, ≥24h thresholds each yield the right unit.
  - Use a fixed `now=` argument; never the real clock.

- `format_uptime`
  - <60s → `Ns`.
  - <1h → `Nm Ss`.
  - <1d → `Nh Mm`.
  - ≥1d → `Nd Hh`.

- `format_stats_lines`
  - Empty manager → header + "(no agents registered)" line.
  - One history-enabled agent + one history-disabled → ordered
    correctly (enabled first, alphabetical), header + separator
    rendered, separator widths match column widths exactly.
  - Memory cell formats:
    - `history_disabled=True` → `"(history disabled)"`.
    - 0 messages → `"0 msgs"`.
    - With budget → contains `"% of <budget>"` and the message count.
    - Without budget → `"~N tok"` only, no `%`.
  - `calls` column right-aligned (verify by stripping and checking
    leading spaces on a longer-than-header value).

Use a `FakeHistoryManager` exposing only `get_stats()` and
`get_user_telemetry()` so tests don't need the full `ChatHistoryManager`.

## Out of scope

- Color / Rich markup (the renderer here is plain text by design).
- Wire-up tests for the CLI / Telegram surfaces — those are glue.

## Acceptance criteria

- [ ] No real clock dependencies (everything takes `now=`).
- [ ] Separator-width regression: an agent with a longer model name
      than the header must shift the separator accordingly.

## Dependencies

- Test bootstrap from Phase 3.6 (`tasks/20260509-171311`).


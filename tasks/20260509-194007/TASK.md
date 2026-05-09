# Unit tests — utils/stats.py (formatters + table render)

- STATUS: CLOSED
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

- [x] No real clock dependencies (everything takes `now=`).
- [x] Separator-width regression: an agent with a longer model name
      than the header must shift the separator accordingly.

## Post-hoc notes

- Landed as `tests/test_stats.py` (27 tests, ~0.4s).
- `format_stats_lines` builds the header with `fmt_row(...).rstrip()`
  but the separator with full padding, so `len(header) != len(sep)`
  even when columns align. Don't assert exact equality — assert
  separator widens with longer model names instead.
- Both `format_relative` and `format_uptime` still call
  `datetime.utcnow()` as a fallback (`stats.py:17` and `:36`); tests
  bypass by always passing `now=`. The deprecation warnings under
  pytest come from this fallback being touched indirectly via
  `format_stats_lines` callbacks — see `tasks/20260509-195010` for
  the cleanup task.

## Dependencies

- Test bootstrap from Phase 3.6 (`tasks/20260509-171311`).


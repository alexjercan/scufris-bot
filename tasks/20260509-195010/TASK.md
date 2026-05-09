# Replace datetime.utcnow() with timezone-aware now(UTC) in history.py

- STATUS: CLOSED
- PRIORITY: 5
- TAGS: chore,deprecation,history

## Why

Python 3.13 deprecates `datetime.utcnow()` (scheduled for removal in
a future version). `pytest` surfaces this as a `DeprecationWarning`
on every test run that touches the history layer (17 warnings across
the current suite, all from one call site).

## Where

- `utils/history.py:226` — inside `record_invocation`:

      self._last_activity[key] = datetime.utcnow()

- `utils/stats.py:17` and `utils/stats.py:36` — defaults inside
  `format_relative` and `format_uptime`:

      now = now or datetime.utcnow()

  Both functions accept an explicit `now=` argument (used by tests
  and callable from elsewhere), so the default is the only call
  site to fix here. Note the test suite for stats already passes
  `now=` everywhere, but the production CLI/Telegram surfaces fall
  through to the default.

These are the only `utcnow()` call sites in the codebase as of
Phase 3.6 follow-ups.

## What to change

Replace with a timezone-aware UTC datetime:

    from datetime import datetime, timezone
    ...
    self._last_activity[key] = datetime.now(timezone.utc)

## Acceptance criteria

- [x] `utils/history.py` no longer calls `datetime.utcnow()`.
- [x] `pytest tests/` produces zero `DeprecationWarning`s related to
      `utcnow`.
- [x] No behaviour change in `/stats` rendering — `format_relative`
      in `utils/stats.py` still computes the right delta. (If it
      does naive subtraction, this task expands to either making
      that subtraction tz-aware or stripping tz at read time. Check
      first; small fix either way.)
- [x] Existing tests still pass.

## Post-hoc notes

- Sweep ended up touching 5 sites, not 3: also `main.py:33`
  (`session_started_at`) and `cli.py:287` (`settings["started_at"]`).
  Both had to flip together — making `format_uptime`'s default
  tz-aware while `started_at` stayed naive would break the
  subtraction.
- `tests/test_stats.py` had a module-level `NOW = datetime(2026, 5, 9,
  12, 0, 0)` (naive) which fed `last_activity` cells in fake
  telemetry dicts. Once the production default went tz-aware, those
  cells subtracted naive↔aware and 8 tests crashed. Fixed by adding
  `tzinfo=timezone.utc` to `NOW`. Tests that pass `now=NOW`
  explicitly stayed naive↔naive, but the in-table `format_relative`
  call inside `format_stats_lines` doesn't forward `now`, so it
  always hits the default.
- Verified zero `utcnow` DeprecationWarnings under `pytest`; ruff
  clean; mypy unchanged (the 21 pre-existing errors are tracked in
  `tasks/20260509-200454`).

## Out of scope

- Auditing the rest of the codebase for other naive datetimes —
  there's only this one call site today.
- Switching to `time.monotonic()` for activity timestamps. Activity
  is wall-clock by intent (it's user-visible in `/stats`).

## Dependencies

None. Standalone, ~5 minute fix + a quick `format_relative`
sanity check.


# Reformat pre-existing files to current ruff style

- STATUS: OPEN
- PRIORITY: 20
- TAGS: cleanup,formatting

## Context

Discovered during step 7 of #10 (`tasks/20260613-091044`). Running
`uv run --active ruff format --check scufris_server tests` flagged
7 files with stale formatting:

- `scufris_server/identity.py`
- `scufris_server/opencode_client.py` *(fixed in step 7 — drift
  was mine from step 3)*
- `scufris_server/routes/identity.py`
- `tests/unit/test_app.py`
- `tests/unit/test_chat_route.py`
- `tests/unit/test_identity.py`
- `tests/unit/test_identity_route.py`

The drift is purely cosmetic — collapsing multi-line expressions
that fit on one line. None of it affects behaviour. Per-step
gates only ran narrow `--check <file>` on touched files, so the
drift accumulated unnoticed across earlier work (#9, #12).

The `ruff check` (lint) gate is clean across the tree; only the
formatter (a separate stylistic check) flags these.

## Why now

- Doesn't block #10's sign-off (step 15 only requires `ruff check`,
  not `ruff format --check`).
- Better to land in its own commit so the diff is "stylistic only,
  no semantic changes" — easier to review.

## Acceptance

- [ ] `uv run --active ruff format scufris_server tests` runs to
      fixed point.
- [ ] `uv run --active ruff format --check scufris_server tests`
      reports clean.
- [ ] Full test suite still green: `uv run --active pytest tests/unit`.
- [ ] Single commit with the reformat; no logic changes.

## Out of scope

- Adding `ruff format --check` to a CI / pre-commit hook (that's a
  separate workflow change).
- Adjusting `pyproject.toml` ruff settings.

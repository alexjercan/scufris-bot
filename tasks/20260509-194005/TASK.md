# Unit tests — utils/telemetry.py

- STATUS: OPEN
- PRIORITY: 18
- TAGS: testing,quality

Sibling of Phase 3.6 — extends the test suite to cover the
JSONL telemetry module added in `tasks/20260509-165516`.

## Scope

`tests/test_telemetry.py`:

- `is_enabled()`
  - Returns False when `SCUFRIS_TELEMETRY` is unset.
  - Returns True for each of `1 / true / yes / on` (case-insensitive).
  - Returns False for anything else (`0`, `false`, `nope`, empty string).
  - Use `monkeypatch.setenv` / `delenv` for isolation.

- `is_refusal(output)`
  - True for `"cannot_handle: foo"`, `"  CANNOT_HANDLE: bar"`,
    leading newlines.
  - False for normal output, empty string, non-string types.

- `begin_turn(user_id)` contextvars
  - Inside the `with` block, `current_turn_id()` and
    `current_user_id()` return the bound values.
  - After exit, both reset to `None`.
  - Nested `begin_turn` calls restore the outer values on inner exit.

- `log_sub_agent_event(...)`
  - When `SCUFRIS_TELEMETRY` is unset → no file is created.
  - When enabled (and pointed at a tmp_path log dir via monkeypatching
    `_LOG_DIR` / `_LOG_PATH`) → one valid JSON line per call, with
    every documented field present and `context_present` reflecting
    `context_chars > 0`.
  - Multiple calls append (don't overwrite).
  - Failures during write are swallowed (e.g. read-only dir): no
    exception escapes.

- `_rotate_if_needed()`
  - Below threshold → no rotation.
  - Above threshold → file renamed to `.1`; pre-existing `.1` is
    overwritten. Use a small `_ROTATE_BYTES` override via monkeypatch
    so the test stays cheap.

## Out of scope

- Concurrency / async. Telemetry is best-effort and the contextvar
  semantics already give us per-task isolation; no need to assert it.

## Acceptance criteria

- [ ] All tests pass.
- [ ] No real file written outside `tmp_path`.
- [ ] `SCUFRIS_TELEMETRY` is restored to its pre-test value (use
      `monkeypatch.setenv/delenv`, never raw `os.environ` mutation).

## Dependencies

- Test bootstrap from Phase 3.6 must be CLOSED first
  (`tasks/20260509-171311`).


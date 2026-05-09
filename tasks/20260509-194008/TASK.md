# Unit tests — pure tools (calculator, datetime_tool)

- STATUS: OPEN
- PRIORITY: 15
- TAGS: testing,quality

Tiny, low-risk tests for the two stdlib-only tools.

## Scope

`tests/test_pure_tools.py`:

### calculator_tool

- Basic arithmetic: `"2 + 2"` → `"4"`, `"10 * (5 + 3)"` → `"80"`,
  `"2 ** 10"` → `"1024"`.
- Built-ins exposed: `"abs(-5)"` → `"5"`, `"max(1,2,3)"` → `"3"`.
- Forbidden access returns an error string starting with
  `"Error evaluating expression"`:
  - `"__import__('os')"` (no builtins)
  - `"open('/etc/passwd')"` (no `open`)
  - Plain `SyntaxError`: `"2 +"`.
- Tool is invoked via `.invoke({"expression": "..."})`, not by
  calling the underlying function — verifies `@tool` wiring.

### datetime_tool

- Default format → matches `r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"`.
- Custom format `"%Y"` → 4-digit year matching the current UTC year
  (compute via `datetime.now(timezone.utc).year`).
- Invalid format → returns string starting with
  `"Error formatting datetime"` (not raised). Trigger with a format
  that crashes `strftime` (e.g. inject a non-string).

## Out of scope

- Localisation, timezone arithmetic — `datetime_tool` is intentionally
  UTC-only.

## Acceptance criteria

- [ ] All tests pass with no I/O.
- [ ] `calculator_tool` security cases assert the *string* (no
      sandbox escape claim implied — `eval` with restricted globals
      is shallow, but the tests document the contract).

## Dependencies

- Test bootstrap from Phase 3.6 (`tasks/20260509-171311`).


# Unit tests — journal_tools (subprocess mocks)

- STATUS: CLOSED
- PRIORITY: 13
- TAGS: testing,quality

`utils/tools/journal_tools.py` is a thin wrapper around the
`today` / `daily` / `macros` external CLIs. Tests should pin down
**how** we shell out (argv shape, optional flag composition) without
actually spawning processes.

## Scope

`tests/test_journal_tools.py`:

Use `monkeypatch` on `utils.tools.journal_tools.subprocess.run` with
a recorder that captures `argv` and returns a configurable
`CompletedProcess`.

- `today_create_tool()` → argv `["today", "--create"]`.
- `today_create_tool("/custom/path")` → argv
  `["today", "/custom/path", "--create"]`.
- `today_create_tool(DEFAULT_DEN_PATH)` → omits the path arg
  (verifies the default-path short-circuit).

- `macros_entry_tool("egg 2pc,12,0,10")` → argv contains
  `"--macros-entry"` and `"egg 2pc,12,0,10"` in order.
- `macros_entry_tool("…", offset=2)` → argv ends with
  `["--offset", "2"]`.

- `macros_lookup_tool("chicken breast 100g")` → argv
  `["macros", "chicken breast 100g"]`.
- `macros_search_tool("chick")` → argv `["macros", "-q", "chick"]`.
- `macros_insert_tool("banana 100g,1,23,0.3")` → argv
  `["macros", "-i", "banana 100g,1,23,0.3"]`.

- `daily_view_tool()` → argv `["daily"]`.
- `daily_view_tool(offset=-1)` → argv `["daily", "--offset", "-1"]`.

- One representative each of: `notes_entry_tool`,
  `notes_filter_tool`, `habits_toggle_tool`, `tasks_entry_tool`,
  `tasks_tomorrow_entry_tool`, `tasks_toggle_tool`,
  `tasks_remove_tool`, `tasks_tomorrow_remove_tool`,
  `weight_entry_tool`. Just argv shape — the goal is regression
  pinning, not exhaustive permutation.

### Error paths

- `subprocess.CalledProcessError` (with `stderr="boom"`) → output
  contains `"Error"` and `"boom"`.
- Generic exception → output starts with `"Unexpected error"`.
- Empty stdout, no error → returns the success message that begins
  with `"✓"`.

## Out of scope

- Anything that requires the actual `today` / `daily` / `macros`
  binaries to exist on PATH.
- The journal file format itself (the wrapped CLIs own that).

## Acceptance criteria

- [x] No real subprocess is spawned.
- [x] Argv-shape assertions cover every tool at least once for the
      default code path; offset-bearing tools also cover the
      `offset != 0` branch.

## Post-hoc notes

- Landed as `tests/test_journal_tools.py` (23 tests, ~0.4s).
- Same `sys.modules["utils.tools.journal_tools"]` trick as the HTTP
  tools file — package `__init__.py` rebinds the attribute.
- Recorder fixture returns a `_FakeCompleted` with `.stdout`/`.stderr`
  rather than the real `subprocess.CompletedProcess` (no `args`/
  `returncode` needed by `run_command`).

## Dependencies

- Test bootstrap from Phase 3.6 (`tasks/20260509-171311`).


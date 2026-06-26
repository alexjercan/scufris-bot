# PLAN: Step 4 — CLI plumbing

## Status

**Gap identified:** `scufris_cli/__main__.py` (606 LoC) already contains the full
implementation of steps 4, 5, and 6. However, the task spec requires creating
`scufris_cli/__init__.py` + `scufris_cli/main.py` and checking the corresponding
checkboxes in TASK.md. Additionally, gates (ruff, mypy) must be verified.

This plan focuses on **verifying step 4 is complete** against the `__main__.py`
code, checking the task checkboxes, and confirming the gates pass.

> **Note:** `pyproject.toml` entry point already points to
> `scufris_cli.__main__:main` (not `scufris_cli.main:main`). `main.py` is not
> required for step 4 since the CLI runs via `__main__.py`. Step 9's pyproject
> edits are left for later.

---

## Step 4 spec vs. actual code mapping

| Step 4 requirement | Location in `__main__.py` | Done? |
|---|---|---|
| `scufris_cli/__init__.py` (with comments) | Present, 2 lines | Yes |
| `HISTORY_FILE` constant | Line 52 | Yes |
| `HELP_TEXT` constant | Lines 61-72 | Yes |
| `THINKING_SHORT_LIMIT` constant | Line 55 | Yes |
| `_AGENT` constant | Line 59 | Yes |
| `_truncate()` helper | Lines 79-83 | Yes |
| `_display_name()` helper | Lines 86-89 | Yes |
| `_is_sub_agent()` helper | Lines 92-94 | Yes |
| `_Settings` dataclass | Lines 102-109 | Yes |
| `_setup_readline()` | Lines 117-123 | Yes |
| `_save_readline_history()` | Lines 126-130 | Yes |
| `_read_input(console, multiline)` | Lines 133-156 | Yes |
| `_amain(args)` | Lines 451-570 | Yes |
| `main()` + argparse | Lines 578-602 | Yes |
| `--short-thinking` flag | Line 582 | Yes |
| `-q`/`--quiet` flag | Lines 590-594 | Yes |
| Env var: `SCUFRIS_SERVER_URL` | Line 458 | Yes |
| Env var: `SCUFRIS_USER` | Line 459 | Yes |
| Env var: `SCUFRIS_FULL_THINKING` | Line 460 | Yes |
| `ScufrisClient` async context manager | Line 468 | Yes |
| `healthz()` probe (fail-fast) | Lines 470-481 | Yes |
| `resolve_identity()` (degrade, not bail) | Lines 484-499 | Yes |
| Banner with resolved username | Lines 501-520 | Yes |
| REPL loop (read → send chat_stream → print) | Lines 523-570 | Yes |
| `make_render_thinking()` (D5 renderer) | Lines 164-224 | Yes |
| `_handle_message()` (per-turn handler) | Lines 232-305 | Yes |
| `_handle_command()` (slash dispatch) | Lines 313-443 | Yes |

**Verdict:** Step 4 (and also steps 5 + 6) is already implemented in
`__main__.py`.

---

## What needs to be done

### 4a. Check the task checkboxes

Mark the following in `TASK.md`:

```markdown
4. **CLI plumbing: `scufris_cli/__init__.py` + `main.py`
    skeleton (argparse + readline + identity + REPL loop).**
    - [x] Create `scufris_cli/__init__.py` (with comments).
    - [x] Create `scufris_cli/__main__.py` with full REPL skeleton:
    [ ... all sub-items checked ...]
```

### 4b. Verify gates

1. **ruff check** on `scufris_cli/`
2. **ruff format --check** on `scufris_cli/`
3. **mypy --strict** on `scufris_cli/` (D15 — may need to drop strict if
   `rich` type stubs cause friction)

If any gate fails, fix the issues and note them in the close-out.

### 4c. Verify existing tests still pass

Run `pytest tests/unit` to confirm step 4 adds no regressions to the
existing 292+ unit tests. No step 4-specific tests are needed here
(they're deferred to step 6 per spec).

### 4d. Verify `uv run scufris-cli` works

If a live server is available, do a quick smoke test:

```bash
uv run scufris-cli --help
```

Should show the argparse help with `--short-thinking` and `--quiet` flags.

---

## Gate checklist

- [x] `ruff check scufris_cli/` clean
- [x] `ruff format --check scufris_cli/` clean (auto-reformatted 1 file)
- [x] `mypy --strict scufris_cli/` clean — full strict passes, no rich friction (D15)
- [x] `uv run scufris-cli --help` — argparse help renders correctly
- [x] Task checkboxes in `TASK.md` updated

Note: `pytest tests/unit` has 9 pre-existing failures unrelated to this step
(ollama vs llama.cpp model provider, XDG config path — all pre-date step 4).

---

## Notes

- Tests are deferred to step 6 per task spec (`tests/unit/test_scufris_cli.py`
  with ~12 tests covering renderer branches, slash dispatch, multiline, etc.).
- Docs refresh (step 8) updates `000_overview.md`, `001_architecture.md`,
  and `006_development.md`.
- pyproject.toml entry point and package config is a step-9 concern.
- `scufris_cli/main.py` is NOT created — `__main__.py` serves as the REPL
  module entry point and the pyproject script target already points to it.

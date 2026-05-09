# Resolve pre-existing mypy errors

- STATUS: OPEN
- PRIORITY: 4
- TAGS: chore,typing

`python3 -m mypy .` reports **21 errors across 9 files** as of the
utcnow + tests cleanup (`tasks/20260509-194005`..`194010`). None
were introduced by the test-coverage work; they're a pre-existing
backlog that's been silently accumulating.

## Inventory

### Missing third-party stubs (environmental, not code defects)

- `experiments/_common.py:49` — `pandas` (`pip install pandas-stubs`)
- `utils/tools/weather_tool.py:11` — `requests` (`types-requests`)
- `tests/test_http_tools.py:13` — same `requests` stub
- These are likely not worth installing into the nix devshell unless
  we also gate CI on `mypy --strict`. Easy win: add to a `[tool.mypy]`
  `ignore_missing_imports` allow-list, or document the gap.

### Real type issues to fix

- `utils/logging.py:32` — implicit `Optional` on `level: int = None`.
  Change signature to `level: int | None = None` (PEP 604).
- `utils/history.py:96` — `get_messages_for_agent` returns
  `list[dict[str, str | list[str | dict]]]` but is annotated as
  `list[dict[str, str]]`. Either widen the annotation to match
  reality (LangChain message dicts can carry tool-call payloads) or
  narrow the return at the call site.
- `utils/telegram.py:55, 57, 86, 126–129, 142, 162, 172` — every
  `update.message.<x>` and `update.effective_user.<x>` access is
  flagged because both are `Optional` in the python-telegram-bot
  type hints. Fix by either:
  - early-return guard at the top of each handler (`if update.message
    is None: return`), or
  - one helper `_require_message(update)` / `_require_user(update)`
    that asserts non-None and is reused everywhere.
- `main.py:44` — `callbacks=[ToolCallbackHandler(...)]` is invariant
  `list[ToolCallbackHandler]` but the API expects `list[BaseCallbackHandler]
  | None`. Fix by annotating the local as
  `callbacks: list[BaseCallbackHandler] = [...]` or switching to a
  `Sequence` parameter type in `create_agent_manager`.
- `main.py:135, 153` — same `update.message` Optional issue as
  `utils/telegram.py`.
- `cli.py:210` — `args = parser.parse_args(...)` then `args = ...`
  reassigns to a `list[str]`; the variable was previously narrowed
  to `str`. Rename the second binding.
- `experiments/turns.py:56` — `bins = ...` needs an explicit
  annotation (likely `dict[int, int]` or `list[int]`).

## Acceptance criteria

- [ ] `python3 -m mypy .` exits 0, OR
- [ ] residual errors are explicitly opted out (e.g.
      `[[tool.mypy.overrides]]` for `experiments/*` and missing
      third-party stubs) with a one-line justification each.
- [ ] No `# type: ignore` blanket suppressions added to source files
      without a `# type: ignore[<code>]  # reason` comment explaining
      why.

## Out of scope

- Switching to `mypy --strict`. Get `--lax` to zero first.
- Adding mypy to a pre-commit hook or CI step. File a separate task
  if/when the baseline is clean.

## Notes

- Some errors will be easier to fix during the planned Phase 4 telegram
  refactor (the `Optional` message guards are pervasive in current
  handlers); this task can be deferred until then if Phase 4 lands
  soon.



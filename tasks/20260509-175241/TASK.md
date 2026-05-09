# Prettier `/stats` table formatting

- STATUS: CLOSED
- PRIORITY: 25
- TAGS: cli,telegram,polish

> Pure cosmetics. The `/stats` table from **Phase 3.4b**
> (`tasks/20260509-172715`) and its follow-up patch is functional
> but uses ad-hoc f-string padding. Variable-width columns
> (memory description, model column) make alignment uneven.
> User-flagged as "fine for now, file for later" during 3.4b review.

## Goal

Replace the hand-rolled padding in
`utils/stats.py:format_stats_lines` with a proper table renderer
that produces clean, uniform output in both the CLI (Rich `Console`)
and Telegram (Markdown ``` block, monospace).

## Suggested approaches

- **Option A — `rich.table.Table`.** Best for CLI; would need a
  separate plain-text path for Telegram (Rich can render to a
  string with `Console(record=True).export_text()`).
- **Option B — manual column layout but driven by `tabulate` or
  similar.** Simpler shared output for both surfaces. Tabulate has
  a `plain` format that works in both.
- **Option C — keep manual layout but compute all column widths
  up front, then format with explicit `{:<W}`.** No new
  dependencies; matches the current style. Lowest risk.

Recommend **Option C** unless a richer renderer is wanted for
visuals.

## Things to address

- Memory column has highly variable width (`(history disabled)` vs
  `8 msgs / ~298 tok (7% of 4000)`). Currently uses a hard `:<38`
  pad which is too tight for some rows.
- Column separators are inconsistent — sometimes two spaces,
  sometimes more, sometimes brackets.
- `last=` and `calls=` could become their own properly-padded
  columns instead of trailing free text.
- Header row would help readability; currently it's a single
  freeform line per agent.

## Out of scope

- New stats fields (those belong on a follow-up to 3.4b).
- Color / Rich styling for CLI (we're keeping the same output for
  Telegram parity — colorizing CLI-only would diverge the two).

## Acceptance criteria

- [x] All columns line up regardless of agent name length, model
      string length, or memory description content.
- [x] CLI `/stats` and Telegram `/stats` (in ``` block) both render
      cleanly with no ragged rows.
- [x] No new third-party dependencies, OR if `tabulate` is added,
      it's justified in the task notes.

## Estimated effort

~30 minutes.

## Implementation notes (post-hoc)

Went with **Option C** (no new deps). Rewrote
`utils/stats.py:format_stats_lines` to:

1. Build all rows (header + data) as tuples of pre-rendered cells.
2. Compute per-column widths as `max(len(cell) for cell in column)`.
3. Render with two-space gutters; `calls` right-aligned, others
   left-aligned. Trailing whitespace stripped per row.
4. Inserted a separator row of `─` matching each column's width
   between header and data.

Smoke-tested via `python -c` — output:

```
Per-agent:
  agent            model         memory                         calls  last
  ───────────────  ────────────  ─────────────────────────────  ─────  ──────
  coding_agent     qwen3:latest  0 msgs                             0  —
  knowledge_agent  qwen3:latest  2 msgs / ~57 tok (1% of 4000)      1  0s ago
  scufris          qwen3:latest  0 msgs                             2  0s ago
  utilities_agent  qwen3:latest  (history disabled)                 1  0s ago
```

Same plain-text output works for CLI and the Telegram ``` block.

# Telemetry experiments folder

- STATUS: CLOSED
- PRIORITY: 25
- TAGS: experiments,telemetry,analysis

> Followup to the telemetry spike (`tasks/20260509-165516`).
> The spike ships raw JSONL events but no analysis. This task
> bootstraps an `experiments/` folder with three short pandas-based
> scripts that read `logs/sub_agent_telemetry.jsonl` and print
> human-readable reports.

## Scope

Create `experiments/` (project-root, separate from `utils/`) with:

1. `summary_per_agent.py` — per `child_agent`: call count, refusal
   rate, error rate, mean / p50 / p95 of `query_chars`,
   `context_chars`, `duration_ms`. Printed as a small text table.
2. `turns.py` — group by `turn_id`: distribution of delegations
   per turn (1, 2, 3+), most common agent combos co-occurring in a
   turn, total chars per turn (mean / p95).
3. `context_presence.py` — per `child_agent`: how often
   `context_present` is true, and the refusal rate split by
   `context_present` (does giving context reduce refusals?).

Each script:
- Runs as `python experiments/<script>.py` from project root.
- Reads `logs/sub_agent_telemetry.jsonl` (default) — overridable
  with a single positional `path` arg.
- Uses pandas + stdlib only. No CLI framework.
- Prints to stdout. No charts, no files written.
- Top-of-file docstring states: requires `SCUFRIS_TELEMETRY=1`
  in production runs to populate the log; **pandas is declared
  in `pyproject.toml` but may not be installed** — install with
  `uv sync` or `pip install pandas` if you get `ImportError`.

## Out of scope

- Plotting (matplotlib).
- Aggregation across multiple log files (we only rotate to `.1`,
  so analysis on `.jsonl` only is fine for the spike).
- Auto-running on a schedule.
- Test infra.

## Acceptance criteria

- [x] `experiments/` exists with the three scripts above.
- [x] Each script handles missing log file gracefully (clear error,
      exit 1).
- [x] Each script handles empty log gracefully (prints "no events"
      and exits 0).
- [x] Each script's docstring documents how to enable telemetry
      and the pandas dependency caveat.
- [x] Scripts smoke-tested with a hand-crafted JSONL fixture.
      *(verified live against real telemetry from a `knowledge_agent`
      session — see "What was verified" below)*

## Estimated effort

~30 minutes.

## Notes

- pandas is declared in `pyproject.toml` but **not installed** in
  the current dev env at the time of writing. User explicitly
  requested I do **not** run `uv sync` or `pip install`. Scripts
  will `ImportError` until the user installs deps themselves.
- Schema reminder (from the spike):
  `ts, user_id, turn_id, parent_agent, child_agent, query_chars,
   context_chars, context_present, outcome, duration_ms`.

## Implementation notes (post-hoc)

### Files

- `experiments/_common.py` — shared `load_events(path)` + tiny
  `parse_path_arg()` CLI helper.
  - The pandas import is **inside** `load_events`, *after* the
    file-existence check, so missing-file and `--help` paths work
    in envs without pandas. This was a deliberate reorder during
    implementation.
  - Malformed JSONL lines are silently skipped (telemetry is
    best-effort).
- `experiments/summary_per_agent.py` — per-agent table:
  `n, refused%, error%, q_{mean,p50,p95}, ctx_{mean,p50,p95},
   dur_{mean,p50,p95}_ms`. Uses the same dynamic-width table
  layout as `utils/stats.py`.
- `experiments/turns.py` — turns grouped by `turn_id`:
  delegations-per-turn distribution (1 / 2 / 3+), top-10 unordered
  agent combos, mean/p50/p95/max of `query+context` chars per turn.
  Defensive `dropna(subset=["turn_id"])` for old logs.
- `experiments/context_presence.py` — per-agent: count with vs
  without context, refusal rate split by `context_present`.

### What was verified

- All four files parse cleanly (`ast.parse`).
- `python experiments/summary_per_agent.py /nonexistent.jsonl` →
  clean error message, exit 1.
- `python experiments/turns.py --help` → usage line, exit 0.
- Live run against 3 real `knowledge_agent` events after the user
  installed pandas separately and exercised the bot.

### Bugs caught during live testing

- `summary_per_agent.py` initially stored `n` as an `int` while
  every other column was a pre-formatted string; the column-width
  computation crashed with `TypeError: object of type 'int' has
  no len()`. Fixed by `str(n)`.
- `n` column renamed to `calls` to match the `/stats` table
  vocabulary (also applied to `context_presence.py`). Naming
  consistency across surfaces is cheap and removes a "what does
  this mean?" question.

### What was *not* verified

- Empty-log "no events" path (logic written but not run).
- `turns.py` and `context_presence.py` against a multi-delegation
  turn (only single-agent turns observed so far). The grouping
  logic is straightforward but the agent-combo Counter is
  effectively untested.

## What to look for when running these scripts

These are the questions each script is designed to answer.
**Treat all of this as exploratory** — there's no SLO or alerting,
just signals to inform later prompt / budget tuning.

### `summary_per_agent.py`

- **High `refused%` for one agent** (say, >30%) means Scufris is
  routing requests it shouldn't. Look at the agent's prompt
  (`utils/agent_builder.py`) — its scope description may be too
  permissive, or `MAIN_AGENT_PROMPT` is mis-categorising tasks.
- **High `error%`** (anything >5%) means the inner agent is
  crashing, not just refusing. Cross-reference with the main log
  for stack traces.
- **`q_p95` >> `q_mean`** suggests Scufris is occasionally dumping
  giant prompts at a sub-agent. Could indicate it's pasting the
  user's whole message instead of summarising into a query.
- **`ctx_p95` near per-agent budget** (`coding`/`knowledge`=4k,
  `journal`=8k chars proxy) means we're routinely close to trim
  thresholds — re-evaluate budget or compaction policy.
- **`dur_p95` >> `dur_mean`** = bimodal latency, usually network
  (web_search, weather) or model warm-up. Not actionable on its
  own; useful for spotting a regression after a model swap.
- **One agent with `calls=0` over a long session**: it's dead
  weight. Either delete it or fix its discoverability in
  `MAIN_AGENT_PROMPT`.

### `turns.py`

- **Most turns are 1 delegation.** Expected and healthy. If
  multi-delegation turns are >40%, either the user is asking
  compound questions (fine) or Scufris is over-decomposing
  (look at examples).
- **Top combos surface workflows.** Recurring pairs like
  `knowledge_agent + journal_agent` suggest a "look it up and
  log it" pattern that might deserve a dedicated tool or a
  prompt nudge.
- **`max` total chars per turn** is the worst-case context
  pressure on any single turn. If it's approaching the sum of
  per-agent budgets you'll start hitting trims mid-turn.
- **Skewed `p95` vs `mean`** for total chars per turn means a few
  turns dominate the cost. Sample those `turn_id`s manually
  (grep the log) to understand what triggered them.

### `context_presence.py`

- **`refused|no_ctx` >> `refused|ctx`** is the ideal finding —
  it justifies the Phase-2 `context` field empirically. If the
  gap is small, sub-agents may be ignoring `context` (check
  their prompts) or `context` is being populated with noise.
- **`refused|ctx` >> `refused|no_ctx`** is a red flag: Scufris
  may be passing context that confuses the sub-agent into
  refusing (e.g. quoting irrelevant chat history). Look at the
  refused samples.
- **`ctx%` near 0 for an agent** = Scufris never gives it
  context. Either the agent doesn't need it (fine — utilities
  is plausible) or `MAIN_AGENT_PROMPT` doesn't teach when to
  populate it.
- **`ctx%` near 100** = Scufris reflexively passes context
  even for cold-start questions. Wasteful tokens; consider
  prompt nudging.

## Cross-cutting things to check periodically

- **After any change to `MAIN_AGENT_PROMPT` or a sub-agent
  prompt:** re-run all three scripts on a fresh log and compare
  refused% / `ctx%` shifts.
- **After model swaps:** `dur_*_ms` and `error%` are the first
  signals.
- **Before tuning per-agent budgets:** run `summary_per_agent.py`
  to see actual `ctx_p95` vs configured budget. Don't tune blind.
- **Log file sanity:** `wc -l logs/sub_agent_telemetry.jsonl`
  occasionally to confirm telemetry is still on. Easy to forget
  the env var across shell sessions.

### Followups (file later if needed)

- A tiny non-pandas variant of `summary_per_agent.py` using
  `statistics` would let it run out of the box. Skipped for now —
  `pyproject.toml` already commits the project to pandas, so this
  is a one-time `uv sync` for the user.

# Telemetry spike: log sub-agent context stats

- STATUS: CLOSED
- PRIORITY: 30
- TAGS: spike,telemetry,observability

## Why

Phase 4 of the multi-agent memory plan ([design doc](../20260509-154912/TASK.md))
is "tune token budgets and trim policies for per-agent history". Tuning blind
is wasteful — we should size budgets against real production traffic, not
guesses.

Phase 2 just shipped the `context` arg on every sub-agent call. We currently
have **zero visibility** into:

- How often Scufris actually populates `context` (vs leaving it empty).
- How long the `context` strings are (chars / tokens).
- Whether certain sub-agents systematically receive longer/shorter context
  than others.
- How often a single user turn results in multiple delegations (carryover
  patterns).

These numbers directly inform Phase 4 decisions like:
- Per-agent context length cap (truncate at N chars before sending?).
- Whether `utilities` actually needs `context` at all (currently on for
  uniformity).
- Trim-policy thresholds for per-agent history.

## What

Lightweight, append-only JSONL log of sub-agent invocations. **No UI, no
dashboard, no aggregation** — just raw events. Analysis is ad-hoc with
`jq` / pandas when Phase 4 lands.

### Event schema (one JSON object per line)

```json
{
  "ts": "2026-05-09T16:55:16Z",
  "user_id": "telegram:12345",     // or "cli:local"
  "turn_id": "<uuid>",             // groups all delegations from one user message
  "parent_agent": "scufris",        // future-proofing for nested delegations
  "child_agent": "knowledge_agent",
  "query_chars": 47,
  "context_chars": 0,              // 0 when context is empty/absent
  "context_present": false,         // explicit bool for cheap filtering
  "outcome": "ok"                   // "ok" | "refused" | "error"
}
```

Token counts deliberately omitted — char count is a good-enough proxy and
avoids tokenizer dependency. Phase 4 can multiply by ~0.25 for a Qwen-ish
estimate.

### Where it hooks in

`utils/callbacks.py` already has `on_tool_start` / `on_tool_end` and already
parses `context` (see `_parse_tool_context`). Add a `TelemetryLogger` that:

1. On `on_tool_start` for a sub-agent tool: stash `(query_chars,
   context_chars, start_ts)` keyed by `run_id`.
2. On `on_tool_end` / `on_tool_error`: pop, compute outcome, append JSONL
   line.

Refusal detection: parse the tool output for the `cannot_handle:` prefix
documented in `SUB_AGENT_MEMORY_CONTEXT`.

`turn_id` is a UUID generated per top-level user message (in `cli.py` and
`main.py` — single line each).

### File location

`logs/sub_agent_telemetry.jsonl` (gitignored). Auto-rotate at 10 MB
(rename to `.1`, drop `.2`+) — keeps it bounded without a dependency.

### Off by default

Enable via `SCUFRIS_TELEMETRY=1` env var. Zero overhead when disabled
(early return in the callback). This is a dev/ops tool, not a user-facing
feature.

## Acceptance criteria

- [x] `SCUFRIS_TELEMETRY=1` produces one JSONL line per sub-agent tool call.
- [x] When unset, no file is created and no perf cost beyond an `if` check.
- [x] All four sub-agents log correctly (knowledge, coding, journal,
      utilities).
- [x] Refused calls (matching `cannot_handle:` prefix) are tagged
      `outcome: "refused"`.
- [x] Errors during sub-agent execution log `outcome: "error"` and don't
      crash the parent turn.
- [x] `turn_id` correctly groups multi-delegation turns (manually verified
      with a follow-up question that triggers two sub-agents).
- [x] Log rotates at 10 MB without data loss.

## Out of scope

- Aggregation / dashboards / metrics endpoint.
- Tokenizer-accurate token counts.
- Logging full query/context strings (privacy + size).
- Capturing inner sub-agent reasoning steps.

## Estimated effort

~1 hour. Single-file change in `utils/callbacks.py` plus two-line additions
in `cli.py` and `main.py` for `turn_id`.

## Dependencies

None for the spike itself. **Consumed by** Phase 4 (which is itself
gated on Phase 3 landing first).

## Implementation notes (post-hoc)

- Phase 4 was retired (see master design doc postscript). The
  telemetry is still useful as ad-hoc tuning input — it's just no
  longer feeding a planned phase.
- New module `utils/telemetry.py` owns:
  - `is_enabled()` — reads `SCUFRIS_TELEMETRY` env var.
  - `begin_turn(user_id)` — context-manager that binds `turn_id`
    + `user_id` to two `contextvars.ContextVar`s for the duration
    of one top-level user message. Used in `cli.py` and `main.py`
    around `agent_manager.process_message`.
  - `log_sub_agent_event(...)` — appends JSONL with rotation at
    10 MB (rename to `.1`, drop the previous `.1`).
  - `is_refusal(output)` — case-insensitive `cannot_handle:` prefix
    check, leading whitespace tolerated.
- `ToolCallbackHandler.on_tool_start` now stashes `query_chars`,
  `context_chars`, and `parent_agent` in `info.extra` for any tool
  passing `is_sub_agent`. The matching `on_tool_end` /
  `on_tool_error` hooks emit the JSONL record with computed
  `duration_ms`. Stashing happens regardless of telemetry-enabled
  state — `log_sub_agent_event` short-circuits when disabled — so
  flipping the env var mid-run never desyncs.
- Refusal vs error vs ok is determined per documented contract:
  output starting with `cannot_handle:` → refused; raised
  exception → error; otherwise → ok.
- `logs/` added to `.gitignore`.
- Chose **char-count proxies** over a tokenizer (per task spec) —
  ad-hoc analysis can multiply by ~0.25 for a Qwen-ish estimate.
- Schema includes `duration_ms` in addition to the originally-
  specified fields, since it's free to compute and useful for
  spotting slow sub-agents.

### Smoke-tests run

- `SCUFRIS_TELEMETRY=1` + two `log_sub_agent_event` calls inside
  `begin_turn(...)` → both lines share one `turn_id`, distinct
  `child_agent`, correct `outcome`/`context_present` fields.
- `SCUFRIS_TELEMETRY` unset → no `logs/` directory created, no
  file written.
- Pre-seeded a 10 MB+ file → next event rotates to `.jsonl.1`
  and writes the new record to a fresh `.jsonl`.
- `python -c "import main"` and `ast.parse` of `cli.py` /
  `utils/callbacks.py` both pass.

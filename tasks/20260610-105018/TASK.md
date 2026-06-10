# Swap /v1/stats data source to OpenCode cost/tokens

- STATUS: OPEN
- PRIORITY: 30
- TAGS: opencode,observability

## Motivation

`/v1/stats` (in `scufris_server/routes/stats.py`) currently reports
counters maintained by `ChatHistoryManager` — message counts,
estimated token usage from tiktoken, per-tool histograms tracked by
`ToolCallbackHandler`. Once OpenCode is the runtime,
`ToolCallbackHandler` is gone and most of those numbers come from
crude estimation.

Meanwhile OpenCode itself emits real numbers: every `session.updated`
and `step-finish` event carries cost (USD), per-stream tokens (input,
output, reasoning, cache read/write), and per-session aggregates are
visible on `GET /session/{id}` and `GET /session`.

This task swaps the data source so the surfaced stats are real and
useful (cost-per-day, real token usage) instead of estimated.

## Scope

### In

- Pick the per-user aggregation point: either accumulate from event
  stream (matches the streaming pipeline) or refetch
  `GET /session/{user_session}` on demand at `/v1/stats` time
  (simpler, lossy across session deletion).
- `/v1/stats` returns:
  - Cost per user (USD).
  - Tokens (input/output/cache) per user.
  - Tool invocation counts (from event stream, not LangChain
    callbacks).
  - Backwards-compat: keep the old field names where reasonable.
- Update `cli.py`'s `/stats` rendering if the field shape changes.

### Out

- Multi-user dashboards / Grafana exporters. Plain HTTP JSON only.
- Historical / time-series persistence. v1: lifetime totals only.

## Acceptance criteria

- [ ] `/v1/stats` no longer references the deleted
      `ToolCallbackHandler` counters.
- [ ] Cost numbers in `/v1/stats` match `GET /session/{id}.cost` from
      OpenCode (within rounding).
- [ ] `cli.py /stats` still renders meaningfully.
- [ ] At least one test asserts the new field shape.

## Open questions

- Whether to expose per-tool cost (would require a per-message
  step-finish accumulator).
- Whether `/v1/clear` should reset the user's accumulated stats too,
  or just the conversation. Probably yes.

## References

- `tasks/20260610-101413/TASK.md` — parent task.
- `tasks/20260610-101413/SCHEMA.md` — event types carrying cost/token
  data.
- `scufris_server/routes/stats.py` — current endpoint.
- `utils/history.py` — current data source.

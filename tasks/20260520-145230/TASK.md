# Tool usage histogram in /stats

- STATUS: OPEN
- PRIORITY: 50
- TAGS: observability,cli,backlog

## New Data Collected

- `ChatHistoryManager` (or a thin wrapper around the callback handler) tracks per-`(user_id, tool_name)` call counts, already partially done via `_invocations`
- Extend to track **all** tools, not just sub-agents — `web_search`, `weather`, `calculator`, `macros_entry`, etc.

## /stats Output Addition

```
Tool usage (this session):
  web_search        ████████  8
  weather           ███       3
  macros_entry      ██        2
  tasks_entry       █         1
  calculator        █         1
```

ASCII bar chart normalized to the max call count — fits in a terminal, works in Telegram monospace block.

## Implementation

- `ToolCallbackHandler.on_tool_end` already fires for every tool — increment a `Counter` keyed by tool name there
- `get_user_telemetry` extended to include the tool counter
- `format_stats_lines` gets a second section below the per-agent table

## Dependencies on User Identity (`20260520-145231`)

**Moderate** dependency. Counters are already `user_id`-keyed today, but with the unified identity layer the meaning sharpens:

- Counts merge correctly across surfaces — "8 web_search calls today" includes calls made from both CLI and Telegram by the same user
- New surface breakdown becomes possible: `/stats --by-surface` could show `web_search: 6 cli, 2 telegram`
- Lifetime counters (not just session) become meaningful once `messages` is persisted (`20260520-145229`) and identity is stable — without identity, "lifetime" is misleading because the same human has two phantom user IDs

## Persistence Tie-in (`20260520-145229`)

For lifetime stats, derive counts on demand from the persisted `messages.tool_calls_json` rather than maintaining a separate counter table:

```sql
SELECT json_extract(tc.value, '$.name') AS tool, COUNT(*)
FROM messages, json_each(messages.tool_calls_json) tc
WHERE user_id = ? AND ts > ?
GROUP BY tool
ORDER BY 2 DESC;
```

In-memory `Counter` stays for current-session stats (fast); SQLite query backs the "all time" view.

## Complexity Estimate

Small. ~1 day of work: increment in the callback, render the histogram, surface in `/stats`. The all-time variant adds another day once persistence lands.

## Open Questions

- **Time window**: session-only, today-only, 7-day, all-time, or all four toggleable? My instinct: session by default, `/stats --since 7d` to widen.
- **Latency / failure tracking**: also show average duration per tool and failure count? Very useful for spotting broken integrations, but bigger scope.
- **Sub-agent attribution**: when the journal sub-agent calls `macros_entry`, is that counted under journal or under the tool? Probably both — group by `(agent, tool)`.
- **Telegram rendering**: monospace block is fine for ASCII bars, but Telegram has 4096-char message limits — cap to top 10 tools by default.

# Knowledge agent: improve reasoning loop, tool selection, and long-term memory

- STATUS: OPEN
- PRIORITY: 0
- TAGS: agents,knowledge

## Goal

The `knowledge_agent` currently wraps `web_search` + `weather` with a
short-history loop. It tends to over-search (calls web_search when the
answer is already in the conversation) and forgets prior lookups
across sessions. Tighten the reasoning loop, teach it to refuse or
defer cleanly, and give it a small long-term memory store so repeated
questions don't re-hit the network.

## Scope

### In
- Rewrite `KNOWLEDGE_AGENT_PROMPT` to make the
  "search vs. answer-from-context vs. refuse" decision explicit, with
  worked examples.
- Add a `recall_tool` backed by a small SQLite table
  (`knowledge_cache(query_hash, query, answer, sources_json, ts)`)
  with TTL per category (weather: 1h, facts: 30d, current-events: 6h).
- Add a `remember_tool` the agent calls explicitly when a lookup
  produced a durable fact worth caching.
- Tool-selection guardrail: if the user's question contains a date
  literal that's already past, prefer `recall_tool` first and only
  fall back to `web_search` on miss.
- Add structured `sources` field to the agent's final answer so the
  caller can render citations.

### Out
- Vector store / embeddings — keyword + hash lookup is enough for v1.
- Multi-hop research workflows (covered by future `research_agent`).
- Replacing the underlying search backend.

## Acceptance criteria
- Asking the same factual question twice in a row results in exactly
  one `web_search` call (second hits `recall_tool`).
- Weather queries respect the 1h TTL — verified by a test that mocks
  the clock.
- Final answers include a `sources: [...]` array when any tool was
  called.
- Eval set of 20 prompts (in `tests/eval/knowledge.jsonl`) shows
  ≥30% reduction in tool calls vs. baseline with no regression in
  answer quality (judged by spot-check rubric in the PR).

## Notes
- Keep the cache local to the user's data dir; don't share across
  users on shared deployments.
- `web_search_tool` already returns structured results — pipe its
  source URLs straight into `sources_json` rather than re-extracting.

## References
- `utils/agent_builder.py:598` — current `create_knowledge_agent`.
- `utils/tools/web_search.py`, `utils/tools/weather_tool.py`.
- `tasks/20260513-121623/TASK.md` — agent v2 spike (may reshuffle
  knowledge agent's responsibilities; coordinate before starting).

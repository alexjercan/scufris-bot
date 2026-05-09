# Phase 3.1 — Rekey ChatHistoryManager by (user_id, agent)

- STATUS: CLOSED
- PRIORITY: 75
- TAGS: phase3,history,refactor

> **Shipped.** All AC pass. Bug found+fixed during smoke test:
> `defaultdict` auto-create on read paths (`get_history`,
> `get_message_count`) was inflating `total_users` in `get_stats`.
> Switched read paths to `.get((key,), [])`. Writes still use the
> defaultdict auto-create (intentional). `SCUFRIS_AGENT = "scufris"`
> exported from `utils.__init__`.

> Implements decision **D1** of the
> [Phase 3 design doc](../20260509-165646/TASK.md). No behaviour change
> for the main flow — this task only changes the internal shape of the
> store and exposes the new API surface that 3.2 / 3.3 will consume.

## Scope

Modify `utils/history.py` so the store is keyed by `(user_id, agent)`
instead of plain `user_id`. Default `agent="scufris"` everywhere so
every existing main-flow callsite keeps working unchanged.

### Concrete changes

1. **Rekey internal store.**
   `_histories: Dict[Tuple[int, str], List[BaseMessage]]`.

2. **Add `agent` kwarg (default `"scufris"`) to existing API.**
   - `add_user_message(user_id, message, agent="scufris")`
   - `add_ai_message(user_id, message, agent="scufris")`
   - `get_history(user_id, agent="scufris")`
   - `get_history_with_new_message(user_id, new_message, agent="scufris")`
   - `get_message_count(user_id, agent="scufris")`

3. **New methods.**
   - `add_messages(user_id, agent, messages: List[BaseMessage], token_budget: int)`
     — append raw messages, then trim to `token_budget` using the
     char-proxy (`chars/4`).
   - `clear_user(user_id)` — delete every key starting with `user_id`,
     return total messages removed.
   - `_trim_by_tokens(key, token_budget)` — pop oldest messages while
     `sum(len(str(m.content)) for m in slice) / 4 > token_budget`.
     Preserves message boundaries.

4. **`clear_history(user_id)` becomes an alias** for `clear_user` —
   kept so existing callsites in `cli.py:175` and `main.py:113` don't
   break.

5. **Update `get_stats`** to add per-agent breakdown:
   ```python
   {
     "total_users": <distinct user_ids>,
     "total_messages": <sum across all (user, agent) slices>,
     "max_history_per_user": <unchanged>,
     "messages_per_agent": {agent_name: count, ...},
   }
   ```

6. **Existing `_trim_history` stays** for the main-flow message-count
   trim. Sub-agent slices use `_trim_by_tokens`. They coexist.

## Out of scope

- Wiring sub-agents to the new methods (3.3).
- `user_id` plumbing (3.2).
- CLI/Telegram UI (3.4).

## Acceptance criteria

- [x] `cli.py` and `main.py` work exactly as today (no `agent=` arg
      added at any callsite yet — defaults carry the main flow).
- [x] `get_history(user_id)` returns the same list as
      `get_history(user_id, agent="scufris")`.
- [x] `add_messages(user_id, "knowledge_agent", msgs, token_budget=4000)`
      stores raw `BaseMessage` objects and trims when char-proxy
      total exceeds `token_budget * 4` chars.
- [x] `clear_user(user_id)` removes both the scufris slice and any
      sub-agent slice in a single call; returns total messages
      removed.
- [x] `get_stats()` reports the per-agent breakdown.

## Estimated effort

~1 hour. Pure data-structure refactor with backward-compatible API.

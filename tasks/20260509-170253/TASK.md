# Phase 3.3 — Wire sub-agent history load+persist in create_sub_agent

- STATUS: CLOSED
- PRIORITY: 75
- TAGS: phase3,agents,memory

> Implements the **C half of Option F** — the actual per-agent memory
> behaviour. Builds on 3.1 (history layer) and 3.2 (`user_id`
> plumbing). After this task ships, sub-agents marked `keeps_history`
> see their previous turns on subsequent calls. See the
> [Phase 3 design doc](../20260509-165646/TASK.md) for the full
> rationale.

## Scope

Extend `create_sub_agent` to accept three new args, compose
`prior + [user_turn]` for the inner invoke, and persist the new
inner-transcript messages on completion.

### Concrete changes in `utils/agent_builder.py`

1. **New `create_sub_agent` parameters.**
   ```python
   def create_sub_agent(
       config: Config,
       name: str,
       system_prompt: str,
       tools: List[BaseTool],
       logger: logging.Logger,
       tool_description: Optional[str] = None,
       *,
       keeps_history: bool = False,
       history_token_budget: int = 4000,
       history_manager: Optional[ChatHistoryManager] = None,
   ) -> BaseTool:
   ```
   When `keeps_history=True` and `history_manager is None`, raise at
   build time (loud failure).

2. **Inside `sub_agent_tool`:**
   ```python
   user_id = (config.get("configurable") or {}).get("user_id")
   if keeps_history:
       if user_id is None:
           raise ValueError(
               f"sub_agent_tool[{name}]: configurable.user_id missing; "
               "check AgentManager.process_message wiring (Phase 3.2)."
           )
       prior = history_manager.get_history(user_id, agent=name)
   else:
       prior = []

   composed_user = compose(context, query)        # unchanged Phase 2 logic
   user_turn = HumanMessage(content=composed_user)
   input_messages = [*prior, user_turn]

   response = agent.invoke({"messages": input_messages})
   all_messages = response.get("messages", [])
   new_messages = all_messages[len(input_messages):]

   if keeps_history:
       history_manager.add_messages(
           user_id,
           agent=name,
           messages=[user_turn, *new_messages],
           token_budget=history_token_budget,
       )

   return last_text(all_messages)
   ```

3. **Update each factory:**
   - `create_coding_agent`: `keeps_history=True, history_token_budget=4000`.
   - `create_knowledge_agent`: `keeps_history=True, history_token_budget=4000`.
   - `create_journal_agent`: `keeps_history=True, history_token_budget=8000`.
   - `create_utilities_agent`: leave defaults (history off).

4. **`setup_scufris` (or wherever the factories are called)** must
   receive and forward the shared `ChatHistoryManager` instance.
   Audit the call chain — likely `cli.py:259` and `main.py:27` now
   need to pass the manager into `setup_scufris` rather than just
   keep it locally.

## Out of scope

- `/clear` UI updates (3.4).
- Thinking-trace `+N prior turns` hint (3.5).
- Tuning the budgets (Phase 4).

## Acceptance criteria

- [x] First `knowledge_agent` call from a fresh user sees an empty
      prior history (cold start). Second call sees the first call's
      user-turn + assistant response in its messages list.
      *(Verified: pre-seeded chartreuse codeword recalled on 2nd call;
      slice grew 2 → 4 messages.)*
- [x] `utilities_agent` history slice never appears in the manager —
      cold-start behaviour preserved. *(Factory leaves
      `keeps_history=False`; tool body skips persist branch.)*
- [x] Long histories trim correctly: after enough turns, the oldest
      messages are evicted such that the slice's char-proxy total
      stays under `token_budget * 4`. *(20×2k char messages ⇒ 7
      retained, 14028 chars ≤ 16000 cap.)*
- [x] `keeps_history=True` without a `history_manager` raises at
      build time with a clear error. *(`ValueError: create_sub_agent('x'):
      keeps_history=True requires a history_manager instance.`)*
- [x] Phase 2 behaviour preserved: `context` still composed into the
      user turn exactly as before. *(Compose path untouched; only
      wraps result in `HumanMessage` instead of dict.)*
- [ ] Manual smoke: ask Scufris a knowledge question, then a clear
      follow-up that delegates to knowledge again. Inspect the
      thinking trace — second call should be visibly shorter
      reasoning thanks to the prior context. *(Deferred to user-driven
      CLI session; programmatic E2E covers persistence behaviour.)*

## Estimated effort

~1.5 hours. Most of the time is in the `setup_scufris` plumbing
audit; the closure logic itself is ~15 lines.

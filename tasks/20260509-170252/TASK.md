# Phase 3.2 — Plumb user_id via RunnableConfig.configurable

- STATUS: CLOSED
- PRIORITY: 75
- TAGS: phase3,plumbing

> **Shipped.** All AC pass. `user_id` is now threaded via
> `RunnableConfig.configurable` from the entry-point handlers
> (`cli.py`, `main.py`) through `AgentManager.process_message` to
> every nested runnable, including `sub_agent_tool`. Verified live
> with a `.func` wrapper spy: invoking `knowledge_agent` with
> `config={"configurable": {"user_id": 12345}}` reaches the tool
> with `config["configurable"]["user_id"] == 12345`. No reader yet
> (Phase 3.3 is the consumer); zero behaviour change today.

> Implements decision **D2** of the
> [Phase 3 design doc](../20260509-165646/TASK.md). Threads the
> caller's `user_id` from the entry-point handlers (CLI / Telegram)
> all the way down to `sub_agent_tool`, using LangChain's standard
> `configurable` mechanism.

## Scope

`sub_agent_tool` already receives a `RunnableConfig` (Phase 2 / langchain
1.x injection). Phase 3.2 fills `configurable.user_id` at the top-level
invoke so the tool can read it. Touches three files; zero behaviour
change because nothing reads it yet (Phase 3.3 is the consumer).

### Concrete changes

1. **`utils/agent.py` — `AgentManager.process_message`.**
   Add a required `user_id: int` parameter (kept positional after
   `messages` for clarity). Pass it via `configurable`:
   ```python
   self.agent.invoke(
       {"messages": messages},
       config={
           "callbacks": self.callbacks,
           "configurable": {"user_id": user_id},
       },
   )
   ```

2. **`cli.py:121`-ish.** Pass `CLI_USER_ID` to `process_message`.

3. **`main.py:68`-ish.** Pass `update.effective_user.id` to
   `process_message`.

4. **No change to `sub_agent_tool` yet.** The plumbing arrives but
   stays unread until 3.3.

## Out of scope

- Reading `user_id` inside `sub_agent_tool` (3.3).
- History layer changes (3.1).

## Acceptance criteria

- [x] CLI runs end-to-end identically to before this task.
- [x] Telegram runs end-to-end identically to before this task.
- [x] A debug `print` (or breakpoint) inside `sub_agent_tool`
      confirms `config["configurable"]["user_id"]` is the caller's
      user_id during a delegation.
- [x] No mypy / lint regressions.

## Estimated effort

~20 minutes. Three-line change in three files plus one small signature
update.

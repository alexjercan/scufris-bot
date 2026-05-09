# Phase 3 design: per-(user, agent) sub-agent history

- STATUS: CLOSED
- PRIORITY: 70
- TAGS: design,agents,memory,phase3

> Design discussion before any code. Spawned from
> [the master memory design doc](../20260509-154912/TASK.md), Phase 3.
> Phase 1 (prompts) and Phase 2 (`context` arg) are already CLOSED.
> Acceptance criteria for *this design doc* live at the bottom; once
> agreed, implementation tasks spin off and code can be written.

## Background — what's already in place

- Sub-agents are stateless workers today. Each invocation is a cold
  start: `[system_prompt, composed_user_message]` where the user
  message is `<context>\n\n---\n\n<query>` (or just `<query>` when
  context is empty/whitespace). See `utils/agent_builder.py:341`.
- Scufris owns user-facing memory via `ChatHistoryManager`
  (`utils/history.py`), keyed by `int` user_id. CLI uses a fixed
  `CLI_USER_ID`; Telegram passes `update.effective_user.id`.
- `/clear` (CLI) and `/clear` (Telegram `clear_history` handler) wipe
  exactly that user's main-history slice.
- Per-agent memory policy already locked in the master design doc:
  - `coding_agent`     — history ON
  - `knowledge_agent`  — history ON
  - `journal_agent`    — history ON (largest window)
  - `utilities_agent`  — history OFF (pure-function calls)

## Goal of Phase 3

Wire each "history-on" sub-agent to read+persist its own per-(user,
agent) message slice. After Phase 3:

- A second `knowledge_agent` call from the same user picks up where
  the first left off, with full inner reasoning continuity (not just
  Scufris's `context` summary).
- `utilities_agent` continues to behave exactly as today (cold start
  every time).
- `/clear` wipes everything for the user — main history AND every
  per-agent slice.

Phase 3 deliberately does NOT include:
- Tuning of window sizes / token budgets (that's Phase 4).
- Telemetry on context stats (separate spike,
  `tasks/20260509-165516/TASK.md`).
- An eval harness (Phase 4).

---

## Design space

Five real decision points. For each, options + recommendation.

### D1. Where does the per-agent history live?

The current `ChatHistoryManager` is keyed by `int` user_id. We need
keying by `(user_id, agent_name)`.

**Option D1.a — Rekey the existing manager.**
Change the internal dict to `Dict[Tuple[int, str], List[BaseMessage]]`.
Main agent uses agent_name=`"main"` (or `None`); sub-agents use their
own name. Single class, single source of truth. Public API gains an
`agent` kwarg with `"main"` as default to keep main-flow callsites
unchanged.

**Option D1.b — Introduce a parallel `SubAgentHistoryManager`.**
Keep `ChatHistoryManager` exactly as-is (handles main only). New class
handles `(user_id, agent_name)`. Two classes; callsites are clearly
disambiguated by which manager they touch.

**Option D1.c — Per-agent manager instances.**
Each `create_*_agent` factory creates its own private
`ChatHistoryManager` instance keyed by user_id only. The sub-agent
closure captures it. No registry; each agent's history lives with the
agent.

**Recommendation: D1.a (rekey).**
- Single source of truth simplifies `/clear` (one method to call).
- The public-API change is a single optional kwarg with a backward-
  compatible default.
- The `(int, str)` key is the natural shape for what we're modelling.
- D1.b duplicates ~80% of the code for marginal naming clarity.
- D1.c makes `/clear` clumsy (need to enumerate all sub-agents to clear
  them) and prevents centralized stats / introspection.

**My take** - I would use "scufris" instead of "main" which is main's agent
name, same as for the other agent's names are like "Knowledge" etc.

> **DECIDED:** Default agent slot for the main agent is `"scufris"`, not
> `"main"`. The architecture sketch and spin-off tasks below use
> `"scufris"` throughout.

### D2. How does `user_id` reach the sub-agent tool?

`sub_agent_tool` today receives `(query, context, config)`. It has no
idea who the user is — and it must, in order to load the right history
slice.

**Option D2.a — Thread via `RunnableConfig.configurable`.**
Top-level `agent.invoke` is called with
`config={"configurable": {"user_id": user_id}, "callbacks": [...]}`.
LangChain propagates `configurable` to every nested runnable. Inside
`sub_agent_tool` we read `config["configurable"]["user_id"]`. This is
the documented LangChain pattern for this exact kind of thing.

**Option D2.b — Closure capture at agent build time.**
`create_sub_agent` takes `user_id` as a build-time arg. But agents are
built *once* at startup, shared across all users. Doesn't work for
Telegram where multiple users hit the same process. Reject.

**Option D2.c — Thread-local / contextvar.**
Set a `ContextVar[int]` at the top of each request, read inside the
tool. Works but is parallel infrastructure to what `configurable` is
literally designed for. Reject in favour of D2.a.

**Recommendation: D2.a.**
Touches three call sites: `AgentManager.process_message`,
`cli.py` invocation, `main.py` invocation. Sub-agent tool reads the
config it already receives.

### D3. What gets stored in per-agent history?

Already decided in the master design doc (Decisions §1): **full inner
transcript, trimmed by token budget**. This is a settled item, recorded
here for completeness.

How to actually capture the inner transcript: `agent.invoke(...)`
returns a dict with `messages` containing `[*input_messages,
*new_messages]`. We compute `new_messages = response["messages"][len(input_messages):]`
and append those to the persisted slice. Implementation detail, not a
decision point.

### D4. Trim policy

Already decided in the master design doc (Decisions §2): **token-budget
based, not message-count**. The exact budget number is **deferred** to
Phase 4 tuning.

For Phase 3 we need a *starting* default. Two implementation choices:

**Option D4.a — char-count proxy.**
Estimate tokens as `chars / 4` (Qwen-ish ratio). Trim from the front
(oldest first), preserving message boundaries (don't split a message).
Zero dependencies, instant.

**Option D4.b — real tokenizer.**
Use the model's tokenizer for accurate counts. More accurate, adds a
dep, slower. Overkill for Phase 3 — we'll iterate the budget number
in Phase 4 anyway and the proxy error is in the noise compared to the
budget itself being a guess.

**Recommendation: D4.a (char proxy)** with starting defaults from the
master design doc:
- `coding_agent`: ~4000 tokens → ~16k chars
- `knowledge_agent`: ~4000 tokens → ~16k chars
- `journal_agent`: ~8000 tokens → ~32k chars
- `utilities_agent`: history off, n/a

Per-agent budget configurable as a `history_token_budget: int` arg on
`create_sub_agent` (default 4000, set higher in `create_journal_agent`).

### D5. `/clear` semantics

Already decided in the master design doc (Decisions §3): **wipes main
history AND every per-agent history for that user. No per-agent clear
sub-command.** Settled, recorded for completeness.

With D1.a (single rekeyed manager), implementation is one new method:
`clear_user(user_id)` removes every entry whose key starts with
`user_id`. The existing `clear_history(user_id)` becomes a thin alias
that delegates to `clear_user`.

---

## Architecture sketch (post-design, pre-implementation)

```
ChatHistoryManager
    _histories: Dict[Tuple[int, str], List[BaseMessage]]
    add_user_message(user_id, msg, agent="scufris")
    add_ai_message(user_id, msg, agent="scufris")
    add_messages(user_id, agent, messages: List[BaseMessage])   # NEW for sub-agents
    get_history(user_id, agent="scufris") -> List[BaseMessage]
    get_history_with_new_message(user_id, msg, agent="scufris") # main-flow only
    clear_user(user_id)                  # NEW — wipes (user_id, *)
    clear_history(user_id)               # alias for clear_user, kept for compat
    _trim(key, token_budget)             # NEW — char-proxy budget trim

create_sub_agent(..., keeps_history: bool = False,
                       history_token_budget: int = 4000,
                       history_manager: Optional[ChatHistoryManager] = None):
    @tool
    def sub_agent_tool(query, context, config):
        user_id = config["configurable"]["user_id"]   # raises if missing
        if keeps_history and history_manager:
            prior = history_manager.get_history(user_id, agent=name)
        else:
            prior = []
        composed_user = compose(context, query)        # unchanged
        input_messages = prior + [HumanMessage(composed_user)]
        response = agent.invoke({"messages": input_messages})
        all_messages = response["messages"]
        new_messages = all_messages[len(input_messages):]
        if keeps_history and history_manager:
            history_manager.add_messages(
                user_id, agent=name,
                messages=[input_messages[-1], *new_messages],   # the user turn + replies
                token_budget=history_token_budget,
            )
        return last_text(all_messages)

create_knowledge_agent / create_coding_agent / create_journal_agent:
    pass keeps_history=True, the right token budget, and the manager.

create_utilities_agent:
    pass keeps_history=False (or just omit — default).

AgentManager.process_message(messages, user_id):     # NEW arg
    self.agent.invoke(
        {"messages": messages},
        config={"callbacks": ..., "configurable": {"user_id": user_id}},
    )

cli.py / main.py:
    Pass user_id when calling process_message.
    /clear → history_manager.clear_user(user_id).
```

A single `ChatHistoryManager` instance is created at startup
(`cli.py:259` / `main.py:27`) and passed both to the main-flow
callsites *and* into each `create_*_agent` factory that opts in.
Wiring this through `setup_scufris` is the only mildly invasive
plumbing change.

## Failure modes and how the design handles them

- **Missing `user_id` in config.** `sub_agent_tool` raises a clear
  `ValueError("sub_agent_tool requires configurable.user_id; check
  AgentManager.process_message wiring")`. Better to fail loudly at dev
  time than silently lose history.
- **History grows unbounded between trims.** Trim runs on *every*
  append, not only when over budget — it's O(n) over the slice but n
  is bounded by budget anyway. No race conditions because everything
  is single-process synchronous Python.
- **Inner transcript contains tool messages with non-string content.**
  `add_messages` stores raw `BaseMessage` instances (no string
  conversion). Trim's char-count uses `len(str(msg.content))` as the
  proxy.
- **User clears history mid-turn.** Not a real concern — user input
  is single-threaded per user. The next turn starts fresh.
- **Sub-agent history "out of sync" with reality** (e.g. user changed
  topic, journal_agent's history still about yesterday's logging).
  Already documented in the master design doc as an accepted soft
  failure; Scufris's `context` is the soft cure. Phase 4 may add an
  age-based eviction if it shows up in practice.

## Acceptance criteria for this design doc

- [x] D1 decision confirmed: rekey existing `ChatHistoryManager`.
      Default agent slot is `"scufris"` (per user feedback on lines
      87–88).
- [x] D2 decision confirmed: thread `user_id` via
      `RunnableConfig.configurable`.
- [x] D4 starting budgets confirmed: 4k/4k/8k tokens for
      coding/knowledge/journal, char-proxy estimator.
- [x] Architecture sketch signed off; spin-off tasks created.

## Spin-off implementation tasks (created once design above is signed off)

1. **History layer rekey.** Modify `ChatHistoryManager` per D1.a.
   Keep main-flow API backward-compatible (default `agent="main"`).
   Add `add_messages`, `clear_user`, char-proxy trim. Update
   `get_stats` to report per-agent breakdown.
2. **Plumb `user_id` via configurable.** Extend
   `AgentManager.process_message` signature; update `cli.py` and
   `main.py` callsites. `sub_agent_tool` reads it from `config`.
3. **Wire sub-agent history loading + persisting.** Add
   `keeps_history`, `history_token_budget`, `history_manager` args
   to `create_sub_agent`; thread through the three factories that
   opt in. Compose `prior + [user_turn]` for invoke; extract and
   persist new messages.
4. **`/clear` updates.** CLI and Telegram handlers call `clear_user`.
   Stats output (CLI `/stats`) shows per-agent message counts.
5. **CLI thinking trace polish (small).** When a sub-agent has prior
   history loaded, render `↳ +N prior turns` under the tool-call line
   alongside the existing `↳ context: ...` line. (Already foreshadowed
   in the master doc, Decisions §5.)

Each spin-off is independently reviewable and small enough to ship in
one sitting.

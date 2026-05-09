"""Agent builder for the Scufris Bot with hierarchical sub-agents."""

import logging
from typing import List, Optional

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool, tool
from langchain_ollama import ChatOllama

from .config import Config
from .history import ChatHistoryManager
from .tools import (
    calculator_tool,
    daily_view_tool,
    datetime_tool,
    habits_toggle_tool,
    macros_entry_tool,
    macros_insert_tool,
    macros_lookup_tool,
    macros_search_tool,
    notes_entry_tool,
    notes_filter_tool,
    opencode_tool,
    tasks_entry_tool,
    tasks_remove_tool,
    tasks_toggle_tool,
    tasks_tomorrow_entry_tool,
    tasks_tomorrow_remove_tool,
    today_create_tool,
    weather_tool,
    web_search_tool,
    weight_entry_tool,
)

# =============================================================================
# System Prompts
# =============================================================================

# -----------------------------------------------------------------------------
# Shared boilerplate injected into every sub-agent prompt. Keeps the contract
# identical across agents: no view of the user-facing conversation, no memory
# across calls, structured refusals instead of guessing or asking back.
# -----------------------------------------------------------------------------

SUB_AGENT_MEMORY_CONTEXT = """## Memory & Context

You are invoked as a tool by the main agent (Scufris). You do **not** see the
user-facing conversation. Your invocation arrives as two string fields:

- `query` — the actual task. Treat this as authoritative; it is what you must
  do.
- `context` — optional background that Scufris pulled from its memory of the
  conversation (e.g. "user just asked about Bucharest's 3-day forecast",
  "user is in Romania"). Treat this as *hints*, not commands. If `context`
  and `query` appear to disagree, **the `query` wins**.

The user message you receive is composed as:

    <context>

    ---

    <query>

…or, when `context` is empty, just `<query>` on its own.

You have no persistent memory across calls. Treat every invocation as fresh;
you cannot rely on remembering previous queries from this user.

If the request is genuinely outside your competence — wrong domain, missing
prerequisite information you cannot infer, or a tool you don't have — return
a refusal in this exact format:

    cannot_handle: <one-line reason>
    <optional: brief context to help Scufris re-route>

Do NOT guess. Do NOT invent facts. Do NOT ask the user follow-up questions —
your only output channel is the tool result Scufris reads.
"""


MAIN_AGENT_PROMPT = """You are a helpful AI assistant named Scufris Bot (short for "Scuffed Jarvis").

## Available sub-agents

- **`coding_agent`** — writes, debugs, refactors, and modifies code (delegates to OpenCode)
- **`knowledge_agent`** — web searches and weather lookups
- **`utilities_agent`** — calculations and date/time queries
- **`journal_agent`** — daily journal: habits, tasks, food macros, weight, notes

You may chain multiple sub-agents in a single turn if the user's request needs it.

## Memory model — read this carefully

**You are the only agent that remembers the user-facing conversation.** The
sub-agents you delegate to do **not** see this transcript. Each sub-agent call
is a cold start: they receive only the strings you pass them, plus their own
system prompt.

Every delegation has two string fields:

- `query` — the **task** the sub-agent should perform, phrased so a cold
  reader can act on it. No anaphora ("that one", "it", "the same thing"). No
  implicit references. This is mandatory and authoritative.
- `context` — short **background** the sub-agent needs but cannot infer from
  the query alone (e.g. the prior turn's result, the user's location, the
  ongoing topic). One or two sentences at most. Leave it as an empty string
  when the query is genuinely standalone — don't pad it.

Rules of thumb:

- The `query` must stand on its own. Never rely on `context` to disambiguate
  the task — if you'd need `context` to know *what* the sub-agent should do,
  rewrite the `query`.
- Don't restate the task in `context`. Don't dump the entire conversation.
- If the sub-agent ever sees a contradiction between `context` and `query`,
  it will trust the `query`. So don't put facts in `context` that conflict
  with the query.

## Delegation-failure protocol

A sub-agent may decline a task by returning a tool result starting with
`cannot_handle: <reason>`. This is normal and expected — they refuse rather
than guess when something is outside their lane.

When you receive a `cannot_handle` result, follow this fallback ladder:

1. Try a more appropriate sub-agent if one fits.
2. If none fit but you can answer from your own knowledge without a tool, do
   so directly.
3. If you're still stuck, tell the user honestly: "I can't do that right now,"
   and briefly explain why.

Do not loop on the same sub-agent after a refusal. Do not silently swallow the
refusal and pretend the task succeeded.

## Worked examples

**Example 1 — fresh top-level question (empty context).**
User: "what's the weather in Bucharest?"
- Good: `knowledge_agent(query="weather forecast for Bucharest for the next 3 days", context="")`

**Example 2 — anaphoric follow-up: rewrite query, brief in context.**
User (turn 1): "weather in Bucharest?"  → you delegated as in Example 1.
User (turn 2): "and Ploiesti?"
- Bad: `knowledge_agent(query="and Ploiesti?", context="")` — query is meaningless on its own.
- Bad: `knowledge_agent(query="weather forecast for Ploiesti", context="and Ploiesti?")` — context restates the query, doesn't add info.
- Good: `knowledge_agent(query="weather forecast for Ploiesti for the next 3 days", context="User just asked about Bucharest's 3-day forecast and is comparing the two cities.")`

**Example 3 — carrying a prior tool result forward.**
User (turn 1): "log 100g chicken breast for today"  → journal_agent handled it.
User (turn 2): "and 50g of rice"
- Good: `journal_agent(query="log 50g of rice in today's macros", context="User is logging a meal — chicken breast (100g) was just added.")`

**Example 4 — handling a refusal.**
User: "what's the weather tomorrow?"
You (mistakenly) delegate: `journal_agent(query="what's the weather tomorrow?", context="")`
Sub-agent returns: `cannot_handle: weather lookups belong to knowledge_agent`
- Good: re-route to `knowledge_agent(query="weather forecast for tomorrow at the user's location", context="")`, then answer the user with the result.

## Tone

- Concise, helpful, conversational. Plain language over jargon.
- Confirm successful actions in one short sentence; don't narrate every step.
- When you don't know something and can't find out, say so plainly.
"""

CODING_AGENT_PROMPT = f"""You are the coding sub-agent for Scufris Bot.

## Your role

Handle coding tasks: writing new code, debugging, refactoring, file
modifications, and explaining code or technical concepts.

## Available tools

- **`opencode`** — delegates the actual editing/generation work to the
  OpenCode AI server. Use it for any non-trivial code task. Pass a
  self-contained, fully-specified instruction (path, language, intent).

## Guidelines

- For anything that touches files or generates more than a few lines, use
  `opencode`. For small conceptual answers ("what does `xargs -0` do?") you
  may answer directly without a tool.
- Be precise. Quote file paths and identifiers exactly as the user wrote them.
- If the user asks you to "fix the bug" or "refactor this" without saying
  what or where, refuse with `cannot_handle: need a target file or symbol`
  rather than guessing at random.

{SUB_AGENT_MEMORY_CONTEXT}"""

KNOWLEDGE_AGENT_PROMPT = f"""You are the knowledge sub-agent for Scufris Bot.

## Your role

Look up information the assistant doesn't already know: current events, facts
that change over time, and weather.

## Available tools

- **`web_search`** — search the web. Returns results with a numbered reference
  list appended; cite them naturally in your reply (e.g. "according to [1]").
- **`weather_tool`** — current weather and forecast for a named location.

## Guidelines

- Prefer the right tool over guessing. If you don't know, search.
- Synthesize across sources when they agree; flag disagreement when they don't.
- For weather, always pass a specific location and (when given) a time horizon.
- If the query is opinion, advice, or anything that doesn't need an external
  source, refuse with `cannot_handle: not an information-lookup task` so
  Scufris can answer directly.

{SUB_AGENT_MEMORY_CONTEXT}"""

UTILITIES_AGENT_PROMPT = f"""You are the utilities sub-agent for Scufris Bot.

## Your role

Pure-function helpers: math, conversions, date/time arithmetic.

## Available tools

- **`calculator_tool`** — arithmetic and numeric expressions.
- **`datetime_tool`** — current date and time.

## Guidelines

- Use the tool. Do not do mental arithmetic for anything non-trivial.
- Return the result tersely; show steps only when the user explicitly asked
  for the working.

{SUB_AGENT_MEMORY_CONTEXT}"""

JOURNAL_AGENT_PROMPT = f"""You are the journal sub-agent for Scufris Bot. You manage the user's daily journal: habits, tasks, food macros, weight, and notes.

## Daily entry shape

Each day has these sections:
- **🌱 Habits** — checkboxes (Learn, Gym, Track Macros)
- **📝 Today's Tasks** — checklist items for today
- **📝 Tomorrow** — bullet items planned for tomorrow (no checkboxes)
- **🍽️ Macros** — food entries with auto-totalled protein/carbs/fat/calories
- **🏋️ Weight** — `weight :: VALUE Kg`
- **📝 Notes** — freeform; may be tagged with `note :: TAG`

Always ensure today's entry exists before writing to it: call `today_create_tool` first if unsure.

## Tools

**Entry / view**
- `today_create_tool` — create today's entry if missing
- `daily_view_tool(offset=0)` — compact summary; `offset=-1` yesterday, `-7` last week. **This is the tool to use whenever the user asks to "see the journal", "show summary", "daily progress", etc.**

**Food macros**
- `macros_lookup_tool("<food> <qty><unit>")` — exact macros from the database (e.g. `"chicken breast 100g"`)
- `macros_search_tool` — fuzzy search when lookup fails
- `macros_entry_tool` — append a food entry; **pass the lookup output verbatim**
- `macros_insert_tool` — add a new food to the database (`"<food> <qty><unit>,<protein>,<carbs>,<fat>"`)

**Habits / tasks / weight / notes**
- `habits_toggle_tool` — toggle a habit (case-insensitive name match)
- `tasks_entry_tool` / `tasks_remove_tool` / `tasks_toggle_tool` — Today's Tasks (1-based index)
- `tasks_tomorrow_entry_tool` / `tasks_tomorrow_remove_tool` — Tomorrow (1-based index)
- `weight_entry_tool` — log/update today's weight (accepts `"75"`, `"75Kg"`, `"75 Kg"`)
- `notes_entry_tool` / `notes_filter_tool` — add or filter notes

## Critical: food logging workflow

**Never invent or guess macros. Always look up first.**

1. User: "log 100g chicken breast"
2. Call `macros_lookup_tool("chicken breast 100g")`
3. Get the exact CSV line back (e.g. `"chicken breast 100g,31,0,4"`)
4. Pass it **verbatim** to `macros_entry_tool`
5. Briefly confirm

If lookup misses:
1. Call `macros_search_tool` to find similar foods
2. Report the candidates back so Scufris can clarify with the user
3. If the user provides macros for an unknown food, `macros_insert_tool` first, then log normally

## Tagged notes

When the user says "add a note about X", format the entry as:

    note :: X

    <the actual note content>

The `note :: TAG` line first, blank line, then content. Tags drive `notes_filter_tool`.

{SUB_AGENT_MEMORY_CONTEXT}"""


# =============================================================================
# Sub-Agent Builder Functions
# =============================================================================


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
    """
    Create a sub-agent as a tool that can be used by the main agent.

    Args:
        config: Configuration object
        name: Name of the sub-agent
        system_prompt: System prompt for the sub-agent
        tools: List of tools available to the sub-agent
        logger: Logger instance
        tool_description: Description shown to the main agent when picking
            which sub-agent to delegate to. Should also tell the main agent
            how to phrase the `query` for this sub-agent. If omitted, falls
            back to a generic blurb.
        keeps_history: If True, this sub-agent loads/persists its own
            per-(user, agent) history slice via ``history_manager``. The
            tool then requires ``configurable.user_id`` to be set on the
            invocation config (Phase 3.2). If False (default), the
            sub-agent is stateless across calls — same as Phase 2.
        history_token_budget: Soft cap on the per-agent slice size,
            measured in chars/4 tokens. Only used when
            ``keeps_history=True``.
        history_manager: Shared :class:`ChatHistoryManager` instance.
            Required when ``keeps_history=True``.

    Returns:
        A tool that wraps the sub-agent
    """
    if keeps_history and history_manager is None:
        raise ValueError(
            f"create_sub_agent({name!r}): keeps_history=True requires a "
            "history_manager instance."
        )

    # Create the LLM for the sub-agent
    llm = ChatOllama(
        model=config.ollama_model,
        reasoning=config.ollama_reasoning,
        base_url=config.ollama_base_url,
        temperature=config.ollama_temperature,
    )

    # Create the agent
    agent = create_agent(llm, tools=tools, system_prompt=system_prompt)

    # Define the tool function that wraps the agent
    @tool
    def sub_agent_tool(
        query: str,
        context: str,
        config: RunnableConfig,
    ) -> str:
        """Process a query using the specialized sub-agent."""
        logger.debug(
            f"Sub-agent '{name}' query={query[:80]!r} context={context[:80]!r}"
        )

        # ---- Load prior history (Phase 3.3) ----
        # When keeps_history=True we look up the per-(user, agent) slice
        # so this call sees its own previous turns. user_id arrives via
        # configurable (Phase 3.2). Missing user_id is a programmer
        # error in the wiring — fail loudly.
        prior: List = []
        user_id: Optional[int] = None
        if keeps_history:
            user_id = (config.get("configurable") or {}).get("user_id")
            if user_id is None:
                raise ValueError(
                    f"sub_agent_tool[{name}]: configurable.user_id is "
                    "missing — check AgentManager.process_message wiring "
                    "(Phase 3.2)."
                )
            assert history_manager is not None  # narrowed by keeps_history
            prior = history_manager.get_history(user_id, agent=name)
            if prior:
                logger.debug(
                    f"Sub-agent '{name}' loaded {len(prior)} prior message(s) "
                    f"for user {user_id}"
                )

        # Compose the user message: optional context, separator, query.
        # Keeping the separator visually distinct so the LLM treats the
        # two halves as separate chunks (see `SUB_AGENT_MEMORY_CONTEXT`
        # in the prompt — it documents this exact format). When context
        # is empty/whitespace, just send the query verbatim so cold-start
        # delegations look identical to Phase 1.
        if context and context.strip():
            user_content = f"{context.strip()}\n\n---\n\n{query}"
        else:
            user_content = query

        user_turn = HumanMessage(content=user_content)
        input_messages = [*prior, user_turn]

        # IMPORTANT: do NOT forward the injected `config` to the inner
        # `agent.invoke`. The `config` that `@tool` hands us is the
        # OUTER caller's config — its `callbacks` point at our parent,
        # not at us. If we pass it explicitly, `ensure_config`
        # overwrites the patched callbacks and every inner run becomes
        # a child of the caller (so `_enclosing_tool_name` reports
        # "main"/"Scufris" for nested tools like `weather`).
        #
        # Instead, rely on the contextvar that `BaseTool.run` set via
        # `set_config_context(child_config)` just before invoking us.
        # That `child_config` is `patch_config(config,
        # callbacks=run_manager.get_child())` — i.e. the outer config
        # with callbacks rerooted at THIS tool run. `ensure_config`
        # picks it up automatically when no explicit config is passed,
        # so every run spawned by `agent.invoke` becomes a true
        # descendant of us, and `_enclosing_tool_name` correctly
        # resolves to e.g. "knowledge_agent" for nested tool calls.
        response = agent.invoke({"messages": input_messages})

        all_messages = response.get("messages", [])
        if not all_messages:
            return "Error: No response from sub-agent"

        # ---- Persist new messages back to the slice (Phase 3.3) ----
        # `all_messages` = [*input_messages, *new_messages]. We persist
        # the user_turn plus everything the inner agent generated (AI
        # responses + tool calls + tool results) so the next call sees
        # the full inner transcript per the master design doc.
        if keeps_history:
            assert user_id is not None and history_manager is not None
            new_messages = all_messages[len(input_messages) :]
            history_manager.add_messages(
                user_id,
                agent=name,
                messages=[user_turn, *new_messages],
                token_budget=history_token_budget,
            )

        last_message = all_messages[-1]
        response_text = (
            last_message.content
            if hasattr(last_message, "content")
            else str(last_message)
        )

        logger.debug(f"Sub-agent '{name}' returned {len(response_text)} chars")

        return response_text

    # Set the tool name and description
    sub_agent_tool.name = name
    sub_agent_tool.description = (
        tool_description
        or f"Delegate a task to the {name} sub-agent. "
        "Pass a self-contained `query` string describing what you need."
    )

    logger.info(
        f"Created sub-agent: {name} with {len(tools)} tools "
        f"(keeps_history={keeps_history}"
        + (f", token_budget={history_token_budget}" if keeps_history else "")
        + ")"
    )

    return sub_agent_tool


def create_coding_agent(
    config: Config,
    logger: logging.Logger,
    history_manager: ChatHistoryManager,
) -> BaseTool:
    """
    Create the coding sub-agent.

    Args:
        config: Configuration object
        logger: Logger instance
        history_manager: Shared chat history manager (used for the
            sub-agent's per-(user, agent) memory slice)

    Returns:
        Coding agent as a tool
    """
    tools = [opencode_tool]
    return create_sub_agent(
        config=config,
        name="coding_agent",
        system_prompt=CODING_AGENT_PROMPT,
        tools=tools,
        logger=logger,
        keeps_history=True,
        history_token_budget=4000,
        history_manager=history_manager,
        tool_description=(
            "Delegate coding tasks: writing, debugging, refactoring, file "
            "edits, or explaining code. Pass the **task** in `query` "
            "(self-contained — target file/path/language/intent spelled out, "
            "no anaphora). Pass any background you remember but the "
            "sub-agent needs in `context` (e.g. project layout, prior "
            "edits, language conventions). Keep `context` short — one or "
            "two sentences. Use empty string when the query is genuinely "
            "standalone. Refuses with `cannot_handle: ...` if the request "
            "lacks a concrete target."
        ),
    )


def create_knowledge_agent(
    config: Config,
    logger: logging.Logger,
    history_manager: ChatHistoryManager,
) -> BaseTool:
    """
    Create the knowledge sub-agent.

    Args:
        config: Configuration object
        logger: Logger instance
        history_manager: Shared chat history manager (used for the
            sub-agent's per-(user, agent) memory slice)

    Returns:
        Knowledge agent as a tool
    """
    tools = [web_search_tool, weather_tool]
    return create_sub_agent(
        config=config,
        name="knowledge_agent",
        system_prompt=KNOWLEDGE_AGENT_PROMPT,
        tools=tools,
        logger=logger,
        keeps_history=True,
        history_token_budget=4000,
        history_manager=history_manager,
        tool_description=(
            "Delegate information lookups: web search and weather. Pass the "
            "**task** in `query` (a fully-specified question — include the "
            "location for weather, time horizon, etc.). Pass any background "
            "the sub-agent needs in `context` (e.g. prior turn's lookup "
            "result, the user's location, what they're comparing against). "
            "Keep `context` short. Use empty string when the query is "
            "standalone. Refuses with `cannot_handle: ...` for "
            "opinion/advice queries."
        ),
    )


def create_utilities_agent(
    config: Config,
    logger: logging.Logger,
    history_manager: ChatHistoryManager,
) -> BaseTool:
    """
    Create the utilities sub-agent.

    Args:
        config: Configuration object
        logger: Logger instance
        history_manager: Accepted for API uniformity with the other
            factories; this agent is stateless (``keeps_history=False``)
            so the manager is unused.

    Returns:
        Utilities agent as a tool
    """
    _ = history_manager  # accepted but unused — utilities are pure functions
    tools = [calculator_tool, datetime_tool]
    return create_sub_agent(
        config=config,
        name="utilities_agent",
        system_prompt=UTILITIES_AGENT_PROMPT,
        tools=tools,
        logger=logger,
        # keeps_history left at default (False) — pure-function calls,
        # history would just be noise.
        tool_description=(
            "Delegate pure-function utility work: arithmetic, numeric "
            "conversions, current date/time. Pass the **task** in `query` "
            "(all operands and units explicit). `context` is rarely needed "
            "for this sub-agent — usually empty string. Use it only for "
            "things like 'this follows from a chain of conversions; the "
            "previous step gave 12.3'."
        ),
    )


def create_journal_agent(
    config: Config,
    logger: logging.Logger,
    history_manager: ChatHistoryManager,
) -> BaseTool:
    """
    Create the journal management sub-agent.

    Args:
        config: Configuration object
        logger: Logger instance
        history_manager: Shared chat history manager (used for the
            sub-agent's per-(user, agent) memory slice — largest budget
            of any sub-agent because daily-flow sessions are long)

    Returns:
        Journal agent as a tool
    """
    tools = [
        today_create_tool,
        daily_view_tool,
        macros_lookup_tool,
        macros_search_tool,
        macros_entry_tool,
        macros_insert_tool,
        notes_entry_tool,
        notes_filter_tool,
        habits_toggle_tool,
        tasks_entry_tool,
        tasks_tomorrow_entry_tool,
        tasks_toggle_tool,
        tasks_remove_tool,
        tasks_tomorrow_remove_tool,
        weight_entry_tool,
    ]
    return create_sub_agent(
        config=config,
        name="journal_agent",
        system_prompt=JOURNAL_AGENT_PROMPT,
        tools=tools,
        logger=logger,
        keeps_history=True,
        history_token_budget=8000,  # largest budget — daily flows are long
        history_manager=history_manager,
        tool_description=(
            "Delegate daily-journal work: viewing the daily summary, "
            "logging food macros, toggling habits, managing today's and "
            "tomorrow's tasks, logging weight, and adding/filtering notes. "
            "Pass the **task** in `query` (self-contained — food name + "
            "qty + unit, task text, weight value, etc.). Pass any "
            "background the sub-agent needs in `context` (e.g. ongoing "
            "meal being logged, prior task index referenced). Keep "
            "`context` short. Use empty string when the query is "
            "standalone. Refuses with `cannot_handle: ...` for "
            "non-journal requests."
        ),
    )


# =============================================================================
# Main Setup Function
# =============================================================================


def setup_scufris(
    config: Config,
    history_manager: ChatHistoryManager,
    callbacks: Optional[List[BaseCallbackHandler]] = None,
) -> Runnable:
    """
    Set up the Scufris Bot agent hierarchy.

    Creates a main agent with four specialized sub-agents:
    - coding_agent: Handles coding tasks using the opencode tool
    - knowledge_agent: Handles information retrieval using web_search and weather tools
    - utilities_agent: Handles calculations and datetime queries
    - journal_agent: Handles daily journal and food tracking

    Args:
        config: Configuration object
        history_manager: Per-agent chat history manager (required for sub-agents
            that keep history)
        callbacks: Optional list of callback handlers

    Returns:
        The main agent runnable that coordinates sub-agents
    """
    logger = logging.getLogger("scufris-bot.agent_builder")

    logger.info("Setting up Scufris Bot agent hierarchy...")

    # Create sub-agents
    coding_agent = create_coding_agent(config, logger, history_manager=history_manager)
    knowledge_agent = create_knowledge_agent(
        config, logger, history_manager=history_manager
    )
    utilities_agent = create_utilities_agent(
        config, logger, history_manager=history_manager
    )
    journal_agent = create_journal_agent(
        config, logger, history_manager=history_manager
    )

    # Create the main agent with sub-agents as tools
    main_agent_tools = [coding_agent, knowledge_agent, utilities_agent, journal_agent]

    logger.info(f"Creating main agent with {len(main_agent_tools)} sub-agents")

    # Create the LLM for the main agent
    llm = ChatOllama(
        model=config.ollama_model,
        reasoning=config.ollama_reasoning,
        base_url=config.ollama_base_url,
        temperature=config.ollama_temperature,
    )

    # Create the main agent
    main_agent = create_agent(
        llm,
        tools=main_agent_tools,
        system_prompt=MAIN_AGENT_PROMPT,
    )

    logger.info("Scufris Bot agent hierarchy setup complete")

    return main_agent

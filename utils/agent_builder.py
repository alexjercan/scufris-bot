"""Agent builder for the Scufris Bot with hierarchical sub-agents."""

import logging
from typing import List, Optional

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool, tool
from langchain_ollama import ChatOllama

from .config import Config
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
user-facing conversation. The `query` string you receive is the entire context
Scufris chose to pass — treat it as self-contained.

You have no persistent memory across calls. Treat every invocation as fresh;
you cannot rely on remembering previous queries from this user.

If the request is genuinely outside your competence — wrong domain, missing
prerequisite information you cannot infer, or a tool you don't have — return a
refusal in this exact format:

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
is a cold start: they receive only the `query` string you write, plus their
own system prompt.

Consequence: every delegation must be a **self-contained task**, intelligible
to a sub-agent that has never seen the conversation. No anaphora ("that one",
"it", "the same thing", "and X?"). No implicit references. If a sub-agent
needs to know the user is currently in Bucharest, the date the user mentioned
two turns ago, or the result of a prior tool call — *you* must include it in
the query. Use your own memory of past tool results to do this.

(A future version of this system will add a separate `context` argument so you
can brief sub-agents without cluttering the query. For now, fold any necessary
context into a clean, fully-specified `query`.)

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

**Example 1 — rephrasing an anaphoric follow-up.**
User (turn 1): "what's the weather in Bucharest?"
You delegate: `knowledge_agent("weather forecast for Bucharest for the next 3 days")`
(sub-agent returns the forecast; you summarise to the user)
User (turn 2): "and Ploiesti?"
- Bad: `knowledge_agent("and Ploiesti?")` — the sub-agent has no idea what "and" refers to.
- Good: `knowledge_agent("weather forecast for Ploiesti for the next 3 days")`.

**Example 2 — carrying over a prior tool result.**
User (turn 1): "log 100g chicken breast for today"
You delegate: `journal_agent("log 100g of chicken breast in today's macros")`
User (turn 2): "and 50g of rice"
- Bad: `journal_agent("and 50g of rice")`.
- Good: `journal_agent("log 50g of rice in today's macros")`.

**Example 3 — handling a refusal.**
User: "what's the weather tomorrow?"
You (mistakenly) delegate: `journal_agent("what's the weather tomorrow?")`
Sub-agent returns: `cannot_handle: weather lookups belong to knowledge_agent`
- Bad: tell the user "I can't check the weather." (You can — you just asked the wrong agent.)
- Good: re-route to `knowledge_agent("weather forecast for tomorrow at the user's location")`,
  then answer the user with the result.

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

    Returns:
        A tool that wraps the sub-agent
    """
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
        config: RunnableConfig,
    ) -> str:
        """Process a query using the specialized sub-agent."""
        logger.debug(f"Sub-agent '{name}' processing query: {query[:100]}...")

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
        _ = config  # kept in the signature so @tool injects it (and so
        # `BaseTool.run` activates the contextvar patch path)
        response = agent.invoke(
            {"messages": [{"role": "user", "content": query}]},
        )

        messages = response.get("messages", [])
        if not messages:
            return "Error: No response from sub-agent"

        last_message = messages[-1]
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

    logger.info(f"Created sub-agent: {name} with {len(tools)} tools")

    return sub_agent_tool


def create_coding_agent(
    config: Config,
    logger: logging.Logger,
) -> BaseTool:
    """
    Create the coding sub-agent.

    Args:
        config: Configuration object
        logger: Logger instance

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
        tool_description=(
            "Delegate coding tasks: writing, debugging, refactoring, file "
            "edits, or explaining code. The `query` must be self-contained "
            "(target file/path/language/intent spelled out) — the sub-agent "
            "does not see this conversation. Refuses with `cannot_handle: "
            "...` if the request lacks a concrete target."
        ),
    )


def create_knowledge_agent(
    config: Config,
    logger: logging.Logger,
) -> BaseTool:
    """
    Create the knowledge sub-agent.

    Args:
        config: Configuration object
        logger: Logger instance

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
        tool_description=(
            "Delegate information lookups: web search and weather. The "
            "`query` must be a fully-specified question — include the "
            "location for weather, the time horizon, and any prior context "
            "the sub-agent needs (it does not see this conversation). "
            "Refuses with `cannot_handle: ...` for opinion/advice queries."
        ),
    )


def create_utilities_agent(
    config: Config,
    logger: logging.Logger,
) -> BaseTool:
    """
    Create the utilities sub-agent.

    Args:
        config: Configuration object
        logger: Logger instance

    Returns:
        Utilities agent as a tool
    """
    tools = [calculator_tool, datetime_tool]
    return create_sub_agent(
        config=config,
        name="utilities_agent",
        system_prompt=UTILITIES_AGENT_PROMPT,
        tools=tools,
        logger=logger,
        tool_description=(
            "Delegate pure-function utility work: arithmetic, numeric "
            "conversions, current date/time. The `query` must contain all "
            "operands and units explicitly — the sub-agent does not see "
            "this conversation."
        ),
    )


def create_journal_agent(
    config: Config,
    logger: logging.Logger,
) -> BaseTool:
    """
    Create the journal management sub-agent.

    Args:
        config: Configuration object
        logger: Logger instance

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
        tool_description=(
            "Delegate daily-journal work: viewing the daily summary, "
            "logging food macros, toggling habits, managing today's and "
            "tomorrow's tasks, logging weight, and adding/filtering notes. "
            "The `query` must be self-contained (food name + qty + unit, "
            "task text, weight value, etc.) — the sub-agent does not see "
            "this conversation. Refuses with `cannot_handle: ...` for "
            "non-journal requests."
        ),
    )


# =============================================================================
# Main Setup Function
# =============================================================================


def setup_scufris(
    config: Config,
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
        callbacks: Optional list of callback handlers

    Returns:
        The main agent runnable that coordinates sub-agents
    """
    logger = logging.getLogger("scufris-bot.agent_builder")

    logger.info("Setting up Scufris Bot agent hierarchy...")

    # Create sub-agents
    coding_agent = create_coding_agent(config, logger)
    knowledge_agent = create_knowledge_agent(config, logger)
    utilities_agent = create_utilities_agent(config, logger)
    journal_agent = create_journal_agent(config, logger)

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

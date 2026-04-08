"""Agent builder for the Scufris Bot with hierarchical sub-agents."""

import logging
from typing import List, Optional

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool
from langchain_ollama import ChatOllama

from .config import Config
from .tools import (
    calculator_tool,
    daily_view_tool,
    datetime_tool,
    macros_entry_tool,
    macros_lookup_tool,
    notes_entry_tool,
    opencode_tool,
    today_create_tool,
    weather_tool,
    web_search_tool,
)

# =============================================================================
# System Prompts
# =============================================================================

MAIN_AGENT_PROMPT = """You are a helpful AI assistant named Scufris Bot (short for "Scuffed Jarvis").

## Available Sub-Agents

You have access to specialized sub-agents to help you assist users:

- **`coding_agent`** - Handles complex coding tasks like writing code, debugging, refactoring, and file modifications
- **`knowledge_agent`** - Searches for information using web search and provides weather information
- **`utilities_agent`** - Performs calculations and provides date/time information
- **`journal_agent`** - Manages daily journal entries, food macros tracking, and notes

## Guidelines

- Be concise, helpful, and friendly in your responses
- Delegate tasks to the appropriate sub-agent based on the user's request
- For coding-related requests, use the `coding_agent`
- For information lookups, web searches, or weather queries, use the `knowledge_agent`
- For calculations or date/time queries, use the `utilities_agent`
- For journal management, food tracking, or daily notes, use the `journal_agent`
- You can use multiple sub-agents if needed to fully answer a user's question

## Tone

- Professional but conversational
- Clear and easy to understand
- Avoid unnecessary jargon unless the context requires it
"""

CODING_AGENT_PROMPT = """You are a specialized coding assistant sub-agent for Scufris Bot.

## Your Role

Handle all coding-related tasks including:
- Writing new code in any programming language
- Debugging existing code
- Refactoring and optimization
- File modifications and code generation
- Explaining code and technical concepts

## Available Tools

- **`opencode`** - Delegate complex coding tasks to OpenCode AI (requires OpenCode server to be running)
  - Use this for file operations, code generation, debugging, and refactoring
  - The OpenCode tool can handle sophisticated coding operations

## Guidelines

- For complex coding tasks, use the `opencode` tool
- Be precise and provide complete solutions
- Explain your reasoning when helpful
- Always verify code correctness and best practices
"""

KNOWLEDGE_AGENT_PROMPT = """You are a specialized knowledge assistant sub-agent for Scufris Bot.

## Your Role

Handle all information retrieval tasks including:
- Web searches for current information, news, and facts
- Weather information queries
- General knowledge questions requiring external sources

## Available Tools

- **`web_search`** - Search the web for current information, news, and facts
- **`weather_tool`** - Get current weather information for any location

## Guidelines

- When you use the `web_search` tool, naturally reference the sources in your response
- You can mention specific references like "According to [1]..." or "Based on the search results from [2]..."
- The web search tool will automatically include a numbered reference list at the end of its output
- For weather queries, use the `weather_tool` to get accurate, current data
- Synthesize information from multiple sources when appropriate
"""

UTILITIES_AGENT_PROMPT = """You are a specialized utilities assistant sub-agent for Scufris Bot.

## Your Role

Handle utility tasks including:
- Mathematical calculations and computations
- Date and time information queries
- Unit conversions and numeric operations

## Available Tools

- **`calculator_tool`** - Perform mathematical calculations
- **`datetime_tool`** - Get current date and time information

## Guidelines

- Use the `calculator_tool` for any mathematical operations
- Use the `datetime_tool` for date/time queries
- Provide clear, accurate results
- Show your work when helpful for complex calculations
"""

JOURNAL_AGENT_PROMPT = """You are a specialized journal management assistant sub-agent for Scufris Bot.

## Your Role

Handle all daily journal and food tracking tasks including:
- Creating and viewing daily journal entries
- Adding food macros entries to the journal
- Looking up nutritional information for food items
- Adding notes to the daily journal
- Managing "the-den" journal entries

## Available Tools

- **`today_create_tool`** - Create today's journal entry if it doesn't exist
- **`daily_view_tool`** - View today's journal entry with a compact summary
- **`macros_lookup_tool`** - Look up nutritional macros for food items (format: "food qty unit")
- **`macros_entry_tool`** - Add food and macros to the Macros section of the journal
- **`notes_entry_tool`** - Add notes to the Notes section of the journal

## Guidelines

- When a user asks to log food, first use `macros_lookup_tool` to get the nutritional info
- After getting macros, use `macros_entry_tool` to add it to the journal
- Food queries should be in format: "<name> <qty><unit>" (e.g., "chicken breast 100g", "egg 2pc")
- Always ensure today's entry exists before adding content (use `today_create_tool` if needed)
- Use `daily_view_tool` to show the current state of the journal
- Be helpful with food tracking and encourage healthy habits
"""


# =============================================================================
# Sub-Agent Builder Functions
# =============================================================================


def create_sub_agent(
    config: Config,
    name: str,
    system_prompt: str,
    tools: List[BaseTool],
    logger: logging.Logger,
) -> BaseTool:
    """
    Create a sub-agent as a tool that can be used by the main agent.

    Args:
        config: Configuration object
        name: Name of the sub-agent
        system_prompt: System prompt for the sub-agent
        tools: List of tools available to the sub-agent
        logger: Logger instance

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
    def sub_agent_tool(query: str) -> str:
        """Process a query using the specialized sub-agent."""
        logger.debug(f"Sub-agent '{name}' processing query: {query[:100]}...")

        response = agent.invoke({"messages": [{"role": "user", "content": query}]})

        messages = response.get("messages", [])
        if not messages:
            return "Error: No response from sub-agent"

        last_message = messages[-1]
        response_text = (
            last_message.content
            if hasattr(last_message, "content")
            else str(last_message)
        )

        return response_text

    # Set the tool name and description
    sub_agent_tool.name = name
    sub_agent_tool.description = f"Delegate tasks to the {name} for specialized handling: {system_prompt.split('Handle all')[1].split('##')[0].strip() if 'Handle all' in system_prompt else 'specialized tasks'}"

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
        macros_entry_tool,
        notes_entry_tool,
    ]
    return create_sub_agent(
        config=config,
        name="journal_agent",
        system_prompt=JOURNAL_AGENT_PROMPT,
        tools=tools,
        logger=logger,
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

"""Web search tool for the agent."""

import logging

from ddgs import DDGS
from langchain_core.tools import Tool

logger = logging.getLogger("scufris-bot.tools.search")


def search_web(query: str) -> str:
    """Search the web using DuckDuckGo and return results.

    Args:
        query: The search query string

    Returns:
        Formatted search results as a string
    """
    try:
        # Use DDGS directly with text search only (more reliable)
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))

            if not results:
                return "No results found for the query."

            # Format results nicely
            formatted_results = []
            for i, result in enumerate(results, 1):
                title = result.get("title", "No title")
                body = result.get("body", "No description")
                url = result.get("href", "")
                formatted_results.append(f"{i}. {title}\n   {body}\n   Source: {url}")

            return "\n\n".join(formatted_results)

    except Exception as e:
        logger.error(f"Web search error: {e}")
        return f"Search failed: {str(e)}"


# Create the web search tool
web_search_tool = Tool(
    name="web_search",
    description=(
        "Search the web for current information. "
        "Use this when you need up-to-date information, facts, news, or "
        "when answering questions about recent events. "
        "Input should be a search query string."
    ),
    func=search_web,
)

"""Web search tool for the agent.

Uses DuckDuckGo via the `ddgs` package. Modernised to the `@tool`
decorator (was a legacy single-input `Tool(...)` — see
``tasks/20260509-165118`` for the sweep that converted the
remaining legacy tools).
"""

import logging

from ddgs import DDGS
from langchain.tools import tool

logger = logging.getLogger("scufris-bot.tools.search")

MAX_RESULTS = 5


@tool("web_search")
def web_search_tool(query: str) -> str:
    """Search the web for current information.

    Use this when you need up-to-date information, facts, news, or when
    answering questions about recent events. Returns the top results with
    short descriptions plus a numbered references section.

    Args:
        query: The search query string.

    Returns:
        Formatted search results with a `📚 References:` block at the end,
        or a message indicating no results / an error string on failure.
    """
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=MAX_RESULTS))

            if not results:
                return "No results found for the query."

            formatted_results = []
            references = []
            for i, result in enumerate(results, 1):
                title = result.get("title", "No title")
                body = result.get("body", "No description")
                url = result.get("href", "")
                formatted_results.append(f"{i}. {title}\n   {body}")
                references.append(f"[{i}] {url}")

            output = "\n\n".join(formatted_results)
            output += "\n\n📚 References:\n" + "\n".join(references)
            return output

    except Exception as e:
        logger.error(f"Web search error: {e}")
        return f"Search failed: {str(e)}"

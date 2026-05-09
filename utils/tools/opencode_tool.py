"""OpenCode tool for the agent to perform coding tasks.

Connects to a locally-running OpenCode server (default
``http://localhost:4096``). Modernised to the `@tool` decorator
(was a legacy single-input `Tool(...)` — see
``tasks/20260509-165118`` for the sweep).
"""

import logging

from langchain.tools import tool
from opencode_ai import APIConnectionError, Opencode

logger = logging.getLogger("scufris-bot.tools.opencode")

OPENCODE_BASE_URL = "http://localhost:4096"
DEFAULT_PROVIDER_ID = "github-copilot"
DEFAULT_MODEL_ID = "claude-sonnet-4.5"


@tool("opencode")
def opencode_tool(task: str) -> str:
    """Run a coding task via the OpenCode AI server.

    Use this when you need to generate code, modify files, or perform
    technical tasks that require deep code understanding (writing,
    debugging, refactoring, codebase analysis, file edits).

    Examples: "Create a Python function to calculate fibonacci numbers",
    "Fix the bug in utils/config.py where the file path is not resolved
    correctly", "Add type hints to all functions in main.py".

    Note: requires the OpenCode server to be running locally on
    ``http://localhost:4096``. If unreachable, returns a clear error
    explaining how to start it.

    Args:
        task: A clear instruction describing the coding task to perform.

    Returns:
        The text output from OpenCode, or a human-readable error message.
    """
    try:
        logger.info(f"Running OpenCode task: {task[:100]}...")

        client = Opencode(base_url=OPENCODE_BASE_URL)

        # The Python SDK requires extra_body={} on session.create to
        # avoid a "Malformed JSON" server-side error.
        session = client.session.create(extra_body={})
        session_id = session.id
        logger.debug(f"Created session: {session_id}")

        response = client.session.chat(
            id=session_id,
            provider_id=DEFAULT_PROVIDER_ID,
            model_id=DEFAULT_MODEL_ID,
            parts=[{"type": "text", "text": task}],
            system="You are a helpful coding assistant. Provide clear, concise responses.",
        )

        # Response shape varies across SDK versions; try common fields.
        response_text = ""
        if hasattr(response, "parts"):
            for part in response.parts:
                if hasattr(part, "text"):
                    response_text += part.text
        elif hasattr(response, "content"):
            response_text = response.content
        else:
            response_text = str(response)

        logger.info(f"OpenCode task completed ({len(response_text)} chars)")

        try:
            client.session.delete(session_id)
            logger.debug(f"Deleted session: {session_id}")
        except Exception as e:
            logger.warning(f"Failed to delete session: {e}")

        return (
            response_text
            if response_text
            else "OpenCode completed but returned no output."
        )

    except APIConnectionError as e:
        logger.error(f"OpenCode connection error: {e}")
        return (
            "❌ Cannot connect to OpenCode server.\n\n"
            "Please start the OpenCode server first:\n"
            "1. Open a new terminal\n"
            "2. Run: opencode serve\n"
            "3. Try your request again\n\n"
            "For more info, see: docs/opencode.md"
        )
    except Exception as e:
        logger.error(f"OpenCode task error: {e}")
        error_msg = str(e)

        if "authentication" in error_msg.lower() or "api key" in error_msg.lower():
            return (
                f"❌ Authentication error: {error_msg}\n\n"
                "Please configure your API provider:\n"
                "1. Run: opencode providers\n"
                "2. Add your API keys\n"
                "3. Try again"
            )
        return (
            f"❌ Error running OpenCode task: {error_msg}\n\n"
            "Make sure OpenCode server is running and properly configured."
        )

"""OpenCode tool for the agent to perform coding tasks."""

import logging

from langchain_core.tools import Tool
from opencode_ai import APIConnectionError, Opencode

logger = logging.getLogger("scufris-bot.tools.opencode")


def run_opencode_task(task: str) -> str:
    """Run an OpenCode task and return the result.

    This tool connects to a running OpenCode server and executes coding tasks.

    Args:
        task: The coding task or instruction for OpenCode to execute

    Returns:
        The result/output from OpenCode
    """
    try:
        logger.info(f"Running OpenCode task: {task[:100]}...")

        # Create OpenCode client (connects to localhost:4096 by default)
        client = Opencode(base_url="http://localhost:4096")

        # Create a new session (need to pass extra_body={} to avoid "Malformed JSON" error)
        session = client.session.create(extra_body={})
        session_id = session.id
        logger.debug(f"Created session: {session_id}")

        # Get default provider and model from config
        # For now, we'll use anthropic/claude-sonnet-4 as default
        # You can make this configurable via environment variables
        provider_id = "github-copilot"
        model_id = "claude-sonnet-4.5"

        # Send the task to OpenCode
        response = client.session.chat(
            id=session_id,
            provider_id=provider_id,
            model_id=model_id,
            parts=[{"type": "text", "text": task}],
            system="You are a helpful coding assistant. Provide clear, concise responses.",
        )

        # Extract the response content
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

        # Clean up session
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

        # Provide helpful error messages based on error type
        if "authentication" in error_msg.lower() or "api key" in error_msg.lower():
            return (
                f"❌ Authentication error: {error_msg}\n\n"
                "Please configure your API provider:\n"
                "1. Run: opencode providers\n"
                "2. Add your API keys\n"
                "3. Try again"
            )
        else:
            return (
                f"❌ Error running OpenCode task: {error_msg}\n\n"
                "Make sure OpenCode server is running and properly configured."
            )


# Create the OpenCode tool
opencode_tool = Tool(
    name="opencode",
    description=(
        "Use OpenCode AI to perform complex coding tasks like writing code, "
        "debugging, refactoring, analyzing codebases, or making file changes. "
        "This is useful when you need to generate code, modify files, or perform "
        "technical tasks that require deep code understanding. "
        "Input should be a clear instruction describing the coding task to perform. "
        "Examples: 'Create a new Python function to calculate fibonacci numbers', "
        "'Fix the bug in utils/config.py where the file path is not resolved correctly', "
        "'Add type hints to all functions in main.py'. "
        "Note: Requires OpenCode server to be running."
    ),
    func=run_opencode_task,
)

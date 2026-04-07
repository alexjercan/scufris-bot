"""Agent management for the Scufris Bot."""

import logging
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import Runnable


class AgentManager:
    """Manages the LLM agent for processing messages."""

    def __init__(
        self,
        agent: Runnable,
        callbacks: Optional[List[BaseCallbackHandler]] = None,
    ):
        """
        Initialize the agent manager.

        Args:
            agent: The main agent runnable (created by setup_scufris)
            callbacks: List of callback handlers
        """
        self.logger = logging.getLogger("scufris-bot.agent")

        self.agent = agent
        self.logger.info("Initialized agent manager")

        # Setup callbacks
        if callbacks is None:
            callbacks = []

        self.callbacks = callbacks
        self.logger.info(f"Loaded {len(self.callbacks)} callback handler(s)")

    async def process_message(self, messages: List[Dict[str, str]]) -> str:
        """
        Process messages through the agent and return the response.

        Args:
            messages: List of message dictionaries with 'role' and 'content' keys
                     This includes the conversation history plus the new message

        Returns:
            The agent's response text

        Raises:
            ValueError: If no response is received from the agent
        """
        self.logger.debug(f"Processing {len(messages)} messages")

        # Invoke the agent with callbacks
        response = self.agent.invoke(
            {"messages": messages},
            config={"callbacks": self.callbacks},
        )

        response_text = self._extract_response_text(response)

        self.logger.info(
            f"Agent response generated (length: {len(response_text)} chars)"
        )

        return response_text

    def _extract_response_text(self, response: Dict[str, Any]) -> str:
        """
        Extract the response text from the agent's response.

        Args:
            response: The agent's response dictionary

        Returns:
            The extracted response text

        Raises:
            ValueError: If no messages are found in the response
        """
        messages = response.get("messages", [])
        if not messages:
            self.logger.error("No messages in agent response")
            raise ValueError("No response from AI")

        last_message = messages[-1]
        response_text = (
            last_message.content
            if hasattr(last_message, "content")
            else str(last_message)
        )

        return response_text


def create_agent_manager(
    agent: Runnable,
    callbacks: Optional[List[BaseCallbackHandler]] = None,
) -> AgentManager:
    """
    Create and return an agent manager instance.

    Args:
        agent: The main agent runnable
        callbacks: Optional list of callback handlers

    Returns:
        Initialized AgentManager instance
    """
    return AgentManager(agent, callbacks)

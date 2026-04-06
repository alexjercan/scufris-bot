"""Agent management for the Scufris Bot."""

import logging
from typing import Any, Dict

from langchain.agents import create_agent
from langchain_ollama import ChatOllama

from .config import Config


class AgentManager:
    """Manages the LLM agent for processing messages."""

    def __init__(self, config: Config):
        """
        Initialize the agent manager.

        Args:
            config: Configuration object
        """
        self.config = config
        self.logger = logging.getLogger("scufris-bot.agent")

        self.logger.info(f"Initializing LLM with model: {config.ollama_model}")
        self.llm = ChatOllama(
            model=config.ollama_model,
            reasoning=config.ollama_reasoning,
            base_url=config.ollama_base_url,
            temperature=config.ollama_temperature,
        )

        self.logger.info("Creating agent with LLM")
        self.agent = create_agent(
            self.llm, tools=[], system_prompt=config.system_prompt
        )

    async def process_message(self, user_message: str) -> str:
        """
        Process a user message through the agent and return the response.

        Args:
            user_message: The message from the user

        Returns:
            The agent's response text

        Raises:
            ValueError: If no response is received from the agent
        """
        self.logger.debug(f"Processing message: {user_message}")

        response = self.agent.invoke(
            {"messages": [{"role": "user", "content": user_message}]}
        )

        response_text = self._extract_response_text(response)

        self.logger.info(
            f"Agent response generated (length: {len(response_text)} chars)"
        )
        self.logger.debug(f"Full response structure: {response}")

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


def create_agent_manager(config: Config) -> AgentManager:
    """
    Create and return an agent manager instance.

    Args:
        config: Configuration object

    Returns:
        Initialized AgentManager instance
    """
    return AgentManager(config)

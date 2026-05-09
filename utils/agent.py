"""Agent management for the Scufris Bot."""

import logging
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import Runnable

from .history import SCUFRIS_AGENT, ChatHistoryManager


class AgentManager:
    """Manages the LLM agent for processing messages."""

    def __init__(
        self,
        agent: Runnable,
        callbacks: Optional[List[BaseCallbackHandler]] = None,
        history_manager: Optional[ChatHistoryManager] = None,
    ):
        """
        Initialize the agent manager.

        Args:
            agent: The main agent runnable (created by setup_scufris)
            callbacks: List of callback handlers
            history_manager: Optional shared history manager. When set,
                ``process_message`` records a per-(user, scufris)
                invocation + last-activity timestamp so ``/stats`` can
                show traffic to the main agent (the main agent isn't
                routed via ``sub_agent_tool``, so it never gets
                ``record_invocation`` from the sub-agent path).
        """
        self.logger = logging.getLogger("scufris-bot.agent")

        self.agent = agent
        self.history_manager = history_manager
        self.logger.info("Initialized agent manager")

        # Setup callbacks
        if callbacks is None:
            callbacks = []

        self.callbacks = callbacks
        self.logger.info(f"Loaded {len(self.callbacks)} callback handler(s)")

    async def process_message(
        self, messages: List[Dict[str, str]], user_id: int
    ) -> str:
        """
        Process messages through the agent and return the response.

        Args:
            messages: List of message dictionaries with 'role' and 'content' keys
                     This includes the conversation history plus the new message
            user_id: Caller's user ID. Threaded into ``configurable`` so
                sub-agent tools can load the right per-(user, agent)
                history slice. See Phase 3.2 / Phase 3.3.

        Returns:
            The agent's response text

        Raises:
            ValueError: If no response is received from the agent
        """
        self.logger.debug(f"Processing {len(messages)} messages for user {user_id}")

        # Record traffic to the main agent so /stats reflects user
        # turns (the sub-agent invocation hook only fires for
        # delegated calls — the main agent is never wrapped in
        # sub_agent_tool).
        if self.history_manager is not None:
            self.history_manager.record_invocation(user_id, SCUFRIS_AGENT)

        # Invoke the agent with callbacks. ``configurable.user_id`` is
        # propagated by LangChain to every nested runnable, including
        # sub-agent tools that need it to key their history.
        response = self.agent.invoke(
            {"messages": messages},
            config={
                "callbacks": self.callbacks,
                "configurable": {"user_id": user_id},
            },
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
    history_manager: Optional[ChatHistoryManager] = None,
) -> AgentManager:
    """
    Create and return an agent manager instance.

    Args:
        agent: The main agent runnable
        callbacks: Optional list of callback handlers
        history_manager: Optional shared history manager — see
            :class:`AgentManager` for behaviour when set.

    Returns:
        Initialized AgentManager instance
    """
    return AgentManager(agent, callbacks, history_manager=history_manager)

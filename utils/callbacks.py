"""Callback handlers for the Scufris Bot agent."""

import logging
import time
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage
from langchain_core.outputs import LLMResult
from telegram import Update

from .logging import truncate_log
from .telegram import TelegramTransport


class ToolCallbackHandler(BaseCallbackHandler):
    """Callback handler for logging tool usage in the agent."""

    def __init__(
        self, telegram_transport: TelegramTransport, update: Optional[Update] = None
    ):
        """
        Initialize the callback handler.

        Args:
            telegram_transport: Telegram transport instance
            update: Telegram update object (optional, can be set later)
        """
        super().__init__()
        self.telegram_transport = telegram_transport
        self.update = update
        self.logger = logging.getLogger("scufris-bot.agent.tools")

        # Track timing for tools and chains
        self._tool_start_time: Optional[float] = None
        self._tool_name: Optional[str] = None
        self._llm_start_time: Optional[float] = None

    def set_update(self, update: Update) -> None:
        """
        Set the current update object for sending status actions.

        Args:
            update: Telegram update object
        """
        self.update = update

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """
        Run when a tool starts running.

        Args:
            serialized: Serialized tool information
            input_str: Input to the tool
            **kwargs: Additional keyword arguments
        """
        self._tool_name = serialized.get("name", "unknown") if serialized else "unknown"
        self._tool_start_time = time.time()

        # Log detailed input at DEBUG level
        self.logger.debug(
            f"Tool '{self._tool_name}' started | input: {truncate_log(input_str, 200)}"
        )

    def on_tool_end(
        self,
        output: ToolMessage,
        **kwargs: Any,
    ) -> None:
        """
        Run when a tool ends running.

        Args:
            output: Output from the tool
            **kwargs: Additional keyword arguments
        """
        # Calculate duration
        duration = time.time() - self._tool_start_time if self._tool_start_time else 0

        # Get output content
        output_content = (
            str(output.content) if hasattr(output, "content") else str(output)
        )
        output_len = len(output_content)
        status = getattr(output, "status", "unknown")

        # Consolidated INFO log
        self.logger.info(
            f"Tool '{self._tool_name}' completed | duration={duration:.2f}s | "
            f"status={status} | output={output_len} chars"
        )

        # Detailed output at DEBUG level
        self.logger.debug(f"Tool output: {truncate_log(output_content, 500)}")

    def on_tool_error(
        self,
        error: Exception,
        **kwargs: Any,
    ) -> None:
        """
        Run when a tool errors.

        Args:
            error: The error that occurred
            **kwargs: Additional keyword arguments
        """
        duration = time.time() - self._tool_start_time if self._tool_start_time else 0
        self.logger.error(
            f"Tool '{self._tool_name}' failed | duration={duration:.2f}s | error: {str(error)}"
        )

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        **kwargs: Any,
    ) -> None:
        """
        Run when LLM starts running.

        Args:
            serialized: Serialized LLM information
            prompts: Prompts sent to the LLM
            **kwargs: Additional keyword arguments
        """
        self._llm_start_time = time.time()
        self.logger.debug("LLM invoked")

    def on_llm_end(
        self,
        response: LLMResult,
        **kwargs: Any,
    ) -> None:
        """
        Run when LLM ends running.

        Args:
            response: Response from the LLM
            **kwargs: Additional keyword arguments
        """
        duration = time.time() - self._llm_start_time if self._llm_start_time else 0
        self.logger.debug(f"LLM response received | duration={duration:.2f}s")

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """
        Run when a chain starts running.

        Args:
            serialized: Serialized chain information
            inputs: Inputs to the chain
            **kwargs: Additional keyword arguments
        """
        chain_name = serialized.get("name", "unknown") if serialized else "unknown"
        self.logger.debug(f"Chain started: {chain_name}")

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """
        Run when a chain ends running.

        Args:
            outputs: Outputs from the chain
            **kwargs: Additional keyword arguments
        """
        self.logger.debug("Chain completed")

    def on_chain_error(
        self,
        error: Exception,
        **kwargs: Any,
    ) -> None:
        """
        Run when a chain errors.

        Args:
            error: The error that occurred
            **kwargs: Additional keyword arguments
        """
        self.logger.error(f"Chain error: {str(error)}")

    def on_agent_action(
        self,
        action: Any,
        **kwargs: Any,
    ) -> None:
        """
        Run when an agent takes an action.

        Args:
            action: The action taken
            **kwargs: Additional keyword arguments
        """
        tool_name = action.tool if hasattr(action, "tool") else "unknown"
        tool_input = action.tool_input if hasattr(action, "tool_input") else {}

        # Move to DEBUG level to reduce verbosity
        self.logger.debug(f"Agent invoking tool '{tool_name}' | input: {tool_input}")

    def on_agent_finish(
        self,
        finish: Any,
        **kwargs: Any,
    ) -> None:
        """
        Run when an agent finishes.

        Args:
            finish: The finish information
            **kwargs: Additional keyword arguments
        """
        self.logger.debug("Agent finished")

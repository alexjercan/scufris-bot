"""Callback handlers for the Scufris Bot agent."""

import logging
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage
from langchain_core.outputs import LLMResult
from telegram import Update

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
        tool_name = serialized.get("name", "unknown") if serialized else "unknown"
        self.logger.info(f"🔧 Tool started: {tool_name}")
        self.logger.info(f"   Input: {input_str}")

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
        self.logger.info("✅ Tool completed")
        self.logger.info(f"   Status: {output.status}")

        # Log the output content
        output_content = (
            str(output.content) if hasattr(output, "content") else str(output)
        )
        # Truncate long outputs for cleaner logs
        max_output_length = 500
        if len(output_content) > max_output_length:
            output_content = output_content[:max_output_length] + "... (truncated)"
        self.logger.info(f"   Output: {output_content}")

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
        self.logger.error(f"❌ Tool error: {str(error)}")

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
        self.logger.debug("🤖 LLM invoked")

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
        self.logger.debug("🤖 LLM response received")

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
        self.logger.debug(f"⛓️  Chain started: {chain_name}")

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
        self.logger.debug("⛓️  Chain completed")

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
        self.logger.error(f"⛓️  Chain error: {str(error)}")

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

        self.logger.info(f"🎯 Agent decision: Use tool '{tool_name}'")
        self.logger.debug(f"   Tool input: {tool_input}")

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
        self.logger.debug("🏁 Agent finished")

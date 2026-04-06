"""Chat history management for the Scufris Bot."""

import logging
from collections import defaultdict
from typing import Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


class ChatHistoryManager:
    """Manages chat history for multiple users."""

    def __init__(self, max_history_per_user: int = 20):
        """
        Initialize the chat history manager.

        Args:
            max_history_per_user: Maximum number of messages to keep per user
        """
        self.logger = logging.getLogger("scufris-bot.history")
        self.max_history_per_user = max_history_per_user

        # Dictionary to store chat history per user ID
        # Format: {user_id: [messages]}
        self._histories: Dict[int, List[BaseMessage]] = defaultdict(list)

        self.logger.info(
            f"Initialized chat history manager (max {max_history_per_user} messages per user)"
        )

    def add_user_message(self, user_id: int, message: str) -> None:
        """
        Add a user message to the history.

        Args:
            user_id: User ID
            message: User's message text
        """
        self._histories[user_id].append(HumanMessage(content=message))
        self._trim_history(user_id)
        self.logger.debug(f"Added user message for user {user_id}")

    def add_ai_message(self, user_id: int, message: str) -> None:
        """
        Add an AI message to the history.

        Args:
            user_id: User ID
            message: AI's response text
        """
        self._histories[user_id].append(AIMessage(content=message))
        self._trim_history(user_id)
        self.logger.debug(f"Added AI message for user {user_id}")

    def get_history(self, user_id: int) -> List[BaseMessage]:
        """
        Get the chat history for a user.

        Args:
            user_id: User ID

        Returns:
            List of messages in the conversation
        """
        return self._histories[user_id].copy()

    def get_history_with_new_message(
        self, user_id: int, new_message: str
    ) -> List[Dict[str, str]]:
        """
        Get the chat history with a new user message appended (for agent input).

        Args:
            user_id: User ID
            new_message: New message from user

        Returns:
            List of message dictionaries suitable for agent input
        """
        # Get existing history
        history = self.get_history(user_id)

        # Convert to dict format and add new message
        messages = [
            {
                "role": "user" if isinstance(msg, HumanMessage) else "assistant",
                "content": msg.content,
            }
            for msg in history
        ]

        # Add the new message
        messages.append({"role": "user", "content": new_message})

        return messages

    def clear_history(self, user_id: int) -> None:
        """
        Clear the chat history for a user.

        Args:
            user_id: User ID
        """
        if user_id in self._histories:
            message_count = len(self._histories[user_id])
            del self._histories[user_id]
            self.logger.info(f"Cleared {message_count} messages for user {user_id}")

    def get_user_count(self) -> int:
        """
        Get the number of users with chat history.

        Returns:
            Number of users
        """
        return len(self._histories)

    def get_message_count(self, user_id: int) -> int:
        """
        Get the number of messages for a user.

        Args:
            user_id: User ID

        Returns:
            Number of messages in history
        """
        return len(self._histories[user_id])

    def _trim_history(self, user_id: int) -> None:
        """
        Trim the history to max_history_per_user messages.

        Args:
            user_id: User ID
        """
        history = self._histories[user_id]
        if len(history) > self.max_history_per_user:
            # Keep only the most recent messages
            removed_count = len(history) - self.max_history_per_user
            self._histories[user_id] = history[-self.max_history_per_user :]
            self.logger.debug(
                f"Trimmed {removed_count} old messages for user {user_id}"
            )

    def get_stats(self) -> Dict[str, any]:
        """
        Get statistics about chat history.

        Returns:
            Dictionary with statistics
        """
        total_messages = sum(len(history) for history in self._histories.values())

        return {
            "total_users": self.get_user_count(),
            "total_messages": total_messages,
            "max_history_per_user": self.max_history_per_user,
        }


def create_history_manager(max_history_per_user: int = 20) -> ChatHistoryManager:
    """
    Create and return a chat history manager instance.

    Args:
        max_history_per_user: Maximum number of messages to keep per user

    Returns:
        Initialized ChatHistoryManager instance
    """
    return ChatHistoryManager(max_history_per_user)

"""Telegram transportation layer for the Scufris Bot."""

import logging
from functools import wraps
from typing import Callable, List

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger("scufris-bot.telegram")

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_SAFE_MESSAGE_LENGTH = 4000


def trim_for_telegram(
    text: str, max_length: int = TELEGRAM_SAFE_MESSAGE_LENGTH
) -> List[str]:
    """
    Trim text to fit Telegram's message length limit.
    Returns a list of message chunks that fit within the limit.

    Args:
        text: The text to trim
        max_length: Maximum length per message (default: 4000 to leave room for formatting)

    Returns:
        List of text chunks, each within the max_length limit
    """
    if len(text) <= max_length:
        return [text]

    chunks = []
    for i in range(0, len(text), max_length):
        j = i + max_length
        chunk = text[i:j]
        chunks.append(chunk)

    return chunks


async def send_long_message(update: Update, text: str, **kwargs) -> None:
    """
    Send a potentially long message to Telegram, splitting if necessary.

    Args:
        update: Telegram update object
        text: The text to send
        **kwargs: Additional arguments to pass to reply_text (e.g., parse_mode)
    """
    chunks = trim_for_telegram(text)

    for i, chunk in enumerate(chunks):
        if i == 0:
            await update.message.reply_text(chunk, **kwargs)
        else:
            await update.message.chat.send_message(chunk, **kwargs)


def restricted(allowed_ids: List[int]) -> Callable:
    """
    Decorator factory to restrict command access to allowed users only.

    Args:
        allowed_ids: List of allowed user IDs

    Returns:
        Decorator function that restricts access
    """

    def decorator(func):
        @wraps(func)
        async def wrapped(
            update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
        ):
            user = update.effective_user
            assert user is not None, "User information is missing in the update"

            user_id = user.id
            username = user.username or user.first_name or "Unknown"

            if user_id not in allowed_ids:
                logger.warning(
                    f"Unauthorized access attempt from user {username} (ID: {user_id})"
                )
                await update.message.reply_text(
                    "⛔ You are not authorized to use this bot."
                )
                return

            logger.info(
                f"Authorized user {username} (ID: {user_id}) accessing {func.__name__}"
            )
            return await func(update, context, *args, **kwargs)

        return wrapped

    return decorator


class TelegramTransport:
    """Transportation layer for handling Telegram messages and responses."""

    def __init__(self, allowed_ids: List[int]):
        """
        Initialize the Telegram transport layer.

        Args:
            allowed_ids: List of allowed user IDs
        """
        self.allowed_ids = allowed_ids
        self.logger = logging.getLogger("scufris-bot.telegram.transport")

    def get_user_info(self, update: Update) -> dict:
        """
        Extract user information from an update.

        Args:
            update: Telegram update object

        Returns:
            Dictionary with user information
        """
        user = update.effective_user
        return {
            "id": user.id,
            "username": user.username or user.first_name or "Unknown",
            "first_name": user.first_name,
            "last_name": user.last_name,
        }

    def get_message_text(self, update: Update) -> str:
        """
        Extract message text from an update.

        Args:
            update: Telegram update object

        Returns:
            Message text
        """
        return update.message.text or ""

    async def send_message(self, update: Update, text: str, **kwargs) -> None:
        """
        Send a message through Telegram, handling long messages.

        Args:
            update: Telegram update object
            text: Text to send
            **kwargs: Additional arguments for reply_text
        """
        await send_long_message(update, text, **kwargs)

    async def send_typing_action(self, update: Update, action: str = "typing") -> None:
        """
        Send typing indicator to show the bot is processing.

        Args:
            update: Telegram update object
        """
        await update.message.chat.send_action(action)

    async def send_error_message(self, update: Update, error: str) -> None:
        """
        Send an error message to the user.

        Args:
            update: Telegram update object
            error: Error message to send
        """
        await update.message.reply_text(f"❌ Error: {error}")

    def is_authorized(self, user_id: int) -> bool:
        """
        Check if a user is authorized to use the bot.

        Args:
            user_id: User ID to check

        Returns:
            True if authorized, False otherwise
        """
        return user_id in self.allowed_ids

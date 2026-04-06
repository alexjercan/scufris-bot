import logging
import os
from functools import wraps
from typing import List

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_ollama import ChatOllama
from rich.logging import RichHandler
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

# Load environment variables from .env file
load_dotenv()

# Configure rich logger
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[
        RichHandler(
            rich_tracebacks=True,
            tracebacks_show_locals=True,
            show_time=True,
            show_path=True,
        )
    ],
)

logger = logging.getLogger("scufris-bot")

# Get configuration from environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_IDS = [
    int(id.strip()) for id in os.getenv("ALLOWED_USER_IDS", "").split(",") if id.strip()
]
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:latest")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

if not TELEGRAM_BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN not found in environment variables")
    raise ValueError("TELEGRAM_BOT_TOKEN not found in environment variables")

if not ALLOWED_IDS:
    logger.critical("ALLOWED_USER_IDS not found in environment variables")
    raise ValueError("ALLOWED_USER_IDS not found in environment variables")

logger.info(
    f"Loaded configuration - Model: {OLLAMA_MODEL}, Base URL: {OLLAMA_BASE_URL}"
)
logger.info(f"Allowed user IDs: {ALLOWED_IDS}")


SYSTEM_PROMPT = "You are a helpful assistant that can answer questions."

logger.info(f"Initializing LLM with model: {OLLAMA_MODEL}")
LLM = ChatOllama(
    model=OLLAMA_MODEL,
    reasoning=True,
    base_url=OLLAMA_BASE_URL,
    temperature=0.7,
)

logger.info("Creating agent with LLM")
AGENT = create_agent(LLM, tools=[], system_prompt=SYSTEM_PROMPT)

# Telegram message length limit
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


def restricted(func):
    """Decorator to restrict command access to allowed users only"""

    @wraps(func)
    async def wrapped(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
    ):
        user = update.effective_user
        assert user is not None, "User information is missing in the update"

        user_id = user.id
        username = user.username or user.first_name or "Unknown"

        if user_id not in ALLOWED_IDS:
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


@restricted
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle regular chat messages using the Ollama LLM Agent"""
    user_message = update.message.text

    if not user_message:
        return

    user = update.effective_user
    username = user.username or user.first_name or "Unknown"
    logger.info(f"Received message from {username}: {user_message[:100]}...")

    try:
        await update.message.chat.send_action("typing")

        logger.debug(f"Invoking agent with message: {user_message}")
        response = AGENT.invoke(
            {"messages": [{"role": "user", "content": user_message}]}
        )

        messages = response.get("messages", [])
        if not messages:
            logger.error("No messages in agent response")
            await update.message.reply_text("❌ No response from AI")
            return

        last_message = messages[-1]
        response_text = (
            last_message.content
            if hasattr(last_message, "content")
            else str(last_message)
        )

        logger.info(f"Agent response generated (length: {len(response_text)} chars)")
        logger.debug(f"Full response structure: {response}")

        await send_long_message(update, response_text)

    except Exception as e:
        logger.error(f"Error getting AI response: {str(e)}", exc_info=True)
        await update.message.reply_text(f"❌ Error getting response from AI:\n{str(e)}")


def main():
    logger.info("Starting Scufris Bot...")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    logger.info("Registering message handlers")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    logger.info("Bot is ready! Starting polling...")
    app.run_polling()


if __name__ == "__main__":
    main()

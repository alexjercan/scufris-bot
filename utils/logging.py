"""Logging configuration for the Scufris Bot."""

import logging
import os

from rich.logging import RichHandler


def truncate_log(text: str, max_length: int = 200) -> str:
    """
    Truncate text for logging with consistent format.

    Args:
        text: Text to truncate
        max_length: Maximum length before truncation (default: 200)

    Returns:
        Truncated text with length indicator if truncated

    Examples:
        >>> truncate_log("Short text", 100)
        "Short text"
        >>> truncate_log("Very long text...", 10)
        "Very long ... (15 chars total)"
    """
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}... ({len(text)} chars total)"


def setup_logging(level: int = None) -> logging.Logger:
    """
    Configure rich logger for the application.

    Sets up logging so that:
    - scufris-bot logs are shown at configured level (default: INFO)
    - All other library logs are shown at ERROR level only

    Args:
        level: Logging level for scufris-bot (default: INFO from env or logging.INFO)

    Returns:
        Configured logger instance

    Environment Variables:
        LOG_LEVEL: Set log level (DEBUG, INFO, WARNING, ERROR, CRITICAL). Default: INFO
    """
    # Get log level from environment if not provided
    if level is None:
        level_str = os.getenv("LOG_LEVEL", "INFO")
        level = getattr(logging, level_str.upper(), logging.INFO)

    # Set root logger to ERROR to suppress debug logs from other libraries
    logging.basicConfig(
        level=logging.ERROR,
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

    # Configure scufris-bot logger to show configured level
    logger = logging.getLogger("scufris-bot")
    logger.setLevel(level)

    # Explicitly set common noisy libraries to ERROR level
    logging.getLogger("httpx").setLevel(logging.ERROR)
    logging.getLogger("httpcore").setLevel(logging.ERROR)
    logging.getLogger("telegram").setLevel(logging.ERROR)
    logging.getLogger("langchain").setLevel(logging.ERROR)
    logging.getLogger("langchain_core").setLevel(logging.ERROR)
    logging.getLogger("langchain_ollama").setLevel(logging.ERROR)

    logger.info(f"Logging configured at {logging.getLevelName(level)} level")

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the given name.

    Args:
        name: Logger name (will be prefixed with "scufris-bot.")

    Returns:
        Logger instance
    """
    return logging.getLogger(f"scufris-bot.{name}")

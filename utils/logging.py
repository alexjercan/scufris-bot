"""Logging configuration for the Scufris Bot."""

import logging

from rich.logging import RichHandler


def setup_logging(level: int = logging.DEBUG) -> logging.Logger:
    """
    Configure rich logger for the application.

    Sets up logging so that:
    - scufris-bot logs are shown at DEBUG level
    - All other library logs are shown at ERROR level only

    Args:
        level: Logging level for scufris-bot (default: logging.DEBUG)

    Returns:
        Configured logger instance
    """
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

    # Configure scufris-bot logger to show DEBUG level
    logger = logging.getLogger("scufris-bot")
    logger.setLevel(level)

    # Explicitly set common noisy libraries to ERROR level
    logging.getLogger("httpx").setLevel(logging.ERROR)
    logging.getLogger("httpcore").setLevel(logging.ERROR)
    logging.getLogger("telegram").setLevel(logging.ERROR)
    logging.getLogger("langchain").setLevel(logging.ERROR)
    logging.getLogger("langchain_core").setLevel(logging.ERROR)
    logging.getLogger("langchain_ollama").setLevel(logging.ERROR)

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

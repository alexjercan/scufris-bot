"""Logging configuration for the Scufris Bot."""

import logging

from rich.logging import RichHandler


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """
    Configure rich logger for the application.

    Args:
        level: Logging level (default: logging.INFO)

    Returns:
        Configured logger instance
    """
    logging.basicConfig(
        level=level,
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

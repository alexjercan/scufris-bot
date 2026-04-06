"""Configuration management for the Scufris Bot."""

import logging
import os
from typing import List

from dotenv import load_dotenv


class Config:
    """Configuration class for the Scufris Bot."""

    def __init__(self):
        """Initialize configuration by loading environment variables."""
        load_dotenv()

        self.logger = logging.getLogger("scufris-bot.config")

        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.allowed_user_ids = self._parse_allowed_ids()

        self.ollama_model = os.getenv("OLLAMA_MODEL", "qwen3:latest")
        self.ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        self.ollama_temperature = float(os.getenv("OLLAMA_TEMPERATURE", "0.7"))
        self.ollama_reasoning = os.getenv("OLLAMA_REASONING", "true").lower() == "true"

        self.system_prompt = os.getenv(
            "SYSTEM_PROMPT", "You are a helpful assistant that can answer questions."
        )

        self._validate()
        self._log_configuration()

    def _parse_allowed_ids(self) -> List[int]:
        """
        Parse allowed user IDs from environment variable.

        Returns:
            List of allowed user IDs
        """
        allowed_ids_str = os.getenv("ALLOWED_USER_IDS", "")
        return [int(id.strip()) for id in allowed_ids_str.split(",") if id.strip()]

    def _validate(self) -> None:
        """Validate required configuration values."""
        if not self.telegram_bot_token:
            self.logger.critical(
                "TELEGRAM_BOT_TOKEN not found in environment variables"
            )
            raise ValueError("TELEGRAM_BOT_TOKEN not found in environment variables")

        if not self.allowed_user_ids:
            self.logger.critical("ALLOWED_USER_IDS not found in environment variables")
            raise ValueError("ALLOWED_USER_IDS not found in environment variables")

    def _log_configuration(self) -> None:
        """Log the loaded configuration."""
        self.logger.info(
            f"Loaded configuration - Model: {self.ollama_model}, "
            f"Base URL: {self.ollama_base_url}"
        )
        self.logger.info(f"Allowed user IDs: {self.allowed_user_ids}")
        self.logger.info(
            f"Ollama settings - Temperature: {self.ollama_temperature}, "
            f"Reasoning: {self.ollama_reasoning}"
        )


def load_config() -> Config:
    """
    Load and return the application configuration.

    Returns:
        Config instance with loaded configuration
    """
    return Config()

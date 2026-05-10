"""Utility modules for the Scufris Bot."""

from .agent import AgentManager, create_agent_manager
from .agent_builder import setup_scufris
from .callbacks import (
    ThinkingCallback,
    ThinkingEvent,
    ToolCallbackHandler,
    display_name,
    is_sub_agent,
)
from .config import Config, load_config
from .history import SCUFRIS_AGENT, ChatHistoryManager, create_history_manager
from .logging import get_logger, setup_logging, truncate_log
from .memory_compactor import (
    CompactionResult,
    Compactor,
    FactEntry,
    LLMCompactor,
    NoopCompactor,
    create_compactor,
)
from .telegram import TelegramTransport, restricted

__all__ = [
    "AgentManager",
    "create_agent_manager",
    "setup_scufris",
    "ThinkingCallback",
    "ThinkingEvent",
    "ToolCallbackHandler",
    "display_name",
    "is_sub_agent",
    "Config",
    "load_config",
    "ChatHistoryManager",
    "create_history_manager",
    "SCUFRIS_AGENT",
    "Compactor",
    "CompactionResult",
    "FactEntry",
    "LLMCompactor",
    "NoopCompactor",
    "create_compactor",
    "setup_logging",
    "get_logger",
    "truncate_log",
    "TelegramTransport",
    "restricted",
]

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
from .config import (
    ClientSection,
    Config,
    HistorySection,
    OllamaSection,
    ResolvedIdentity,
    ServerSection,
    TelegramSection,
    UserIdentity,
    UserJournal,
    UserSection,
    config_search_paths,
    load_config,
    parse_config,
    resolve_user_id,
)
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
    "ClientSection",
    "Config",
    "HistorySection",
    "OllamaSection",
    "ResolvedIdentity",
    "ServerSection",
    "TelegramSection",
    "UserIdentity",
    "UserJournal",
    "UserSection",
    "config_search_paths",
    "load_config",
    "parse_config",
    "resolve_user_id",
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

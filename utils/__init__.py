"""Utility modules for the Scufris Bot."""

from .agent import (
    DEFAULT_SYSTEM_PROMPT_BASE,
    AgentManager,
    ThinkingCallback,
    create_agent_manager,
)
from .callbacks import (
    ThinkingEvent,
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
from .opencode_client import (
    OpenCodeClient,
    OpenCodeError,
    OpenCodeSessionError,
    OpenCodeStaleSessionError,
)
from .opencode_events import (
    EventMapperState,
    extract_text_delta,
    map_opencode_event,
)
from .session_store import (
    DEFAULT_FILENAME as SESSION_STORE_FILENAME,
)
from .session_store import (
    SessionStore,
    default_session_store_path,
)
from .telegram import TelegramTransport, restricted

__all__ = [
    "AgentManager",
    "DEFAULT_SYSTEM_PROMPT_BASE",
    "ThinkingCallback",
    "create_agent_manager",
    "ThinkingEvent",
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
    "OpenCodeClient",
    "OpenCodeError",
    "OpenCodeSessionError",
    "OpenCodeStaleSessionError",
    "EventMapperState",
    "extract_text_delta",
    "map_opencode_event",
    "SessionStore",
    "SESSION_STORE_FILENAME",
    "default_session_store_path",
    "setup_logging",
    "get_logger",
    "truncate_log",
    "TelegramTransport",
    "restricted",
]

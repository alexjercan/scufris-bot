"""Wires up the agent runtime for the HTTP server.

Mirrors ``cli.py`` — same config loading, same history manager, same
``setup_scufris`` call. The only differences are:

  * No Telegram dependency (``require_telegram=False``).
  * The ``ToolCallbackHandler`` registered on the manager has no
    ``on_thinking`` listener — per-request callbacks (one per SSE
    stream) carry the listener instead, so each client gets only its
    own events.
  * The history manager's compaction event sink is the global
    dispatcher in :mod:`scufris_server.locks`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from utils import (
    AgentManager,
    ChatHistoryManager,
    ToolCallbackHandler,
    create_agent_manager,
    create_compactor,
    create_history_manager,
    load_config,
    setup_scufris,
)
from utils.config import Config

from .locks import dispatch_event


@dataclass
class Runtime:
    """Container for the long-lived process-wide agent state."""

    config: Config
    history_manager: ChatHistoryManager
    agent_manager: AgentManager


def build_runtime() -> Runtime:
    """Construct the process-wide runtime. Called once on app startup."""
    logger = logging.getLogger("scufris-server.bootstrap")

    config = load_config(require_telegram=False)
    history_manager = create_history_manager(
        config.max_history_per_user, compactor=create_compactor()
    )
    main_agent = setup_scufris(config=config, history_manager=history_manager)

    # Manager-level callback handler is just for server-side logging —
    # per-request handlers (one per SSE stream) own the user-facing
    # listener so events fan out only to the requesting client.
    callback_handler = ToolCallbackHandler()

    # Compaction events are emitted by the history manager without a
    # user id; route them through the dispatcher which looks at the
    # current-user ContextVar.
    history_manager.set_event_sink(dispatch_event)

    agent_manager = create_agent_manager(
        agent=main_agent,
        callbacks=[callback_handler],
        history_manager=history_manager,
    )
    logger.info("scufris runtime ready")
    return Runtime(
        config=config,
        history_manager=history_manager,
        agent_manager=agent_manager,
    )

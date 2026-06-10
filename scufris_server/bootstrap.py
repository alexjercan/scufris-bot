"""Wires up the agent runtime for the HTTP server.

After the OpenCode-runtime swap (task ``20260610-101413``) the runtime
holds:

- an :class:`OpenCodeClient` (closed by the FastAPI lifespan),
- an :class:`AgentManager` driving turns through that client,
- a :class:`ChatHistoryManager` used for facts / summary / stats but
  no longer for raw history (OpenCode owns that per session).
- a :class:`SessionStore` persisting the per-user OpenCode session
  id map so it survives ``scufris-server`` restarts (task
  ``20260610-105007``).

The compactor defaults to :class:`NoopCompactor` so we don't pull
LangChain in at runtime. Rewriting the compactor to use OpenCode's
own summarize endpoint (or direct Ollama HTTP) is filed as
``tasks/20260610-105002``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from utils import (
    AgentManager,
    ChatHistoryManager,
    Config,
    NoopCompactor,
    OpenCodeClient,
    SessionStore,
    create_agent_manager,
    create_history_manager,
    load_config,
)

from .locks import dispatch_event

DEFAULT_OPENCODE_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_OPENCODE_PROVIDER = "github-copilot"
DEFAULT_OPENCODE_MODEL = "claude-sonnet-4"

# v1 policy: disable OpenCode's task / todo tools so the assistant
# doesn't try to spawn its own sub-agents. Skills (loaded via OpenCode's
# AGENTS.md mechanism in a separate task) will replace that pattern.
DEFAULT_OPENCODE_TOOLS = {
    "task": False,
    "todoread": False,
    "todowrite": False,
}


@dataclass
class Runtime:
    """Container for the long-lived process-wide agent state."""

    config: Config
    history_manager: ChatHistoryManager
    agent_manager: AgentManager
    opencode_client: OpenCodeClient
    session_store: SessionStore


def _resolve_opencode_base_url() -> str:
    """Read ``OPENCODE_BASE_URL`` or fall back to the local-default."""
    return os.environ.get("OPENCODE_BASE_URL") or DEFAULT_OPENCODE_BASE_URL


def build_runtime() -> Runtime:
    """Construct the process-wide runtime. Called once on app startup."""
    logger = logging.getLogger("scufris-server.bootstrap")

    config = load_config(require_telegram=False)

    history_manager = create_history_manager(
        config.history.max_per_user,
        compactor=NoopCompactor(),
    )

    base_url = _resolve_opencode_base_url()
    provider_id = os.environ.get("OPENCODE_PROVIDER_ID") or DEFAULT_OPENCODE_PROVIDER
    model_id = os.environ.get("OPENCODE_MODEL_ID") or DEFAULT_OPENCODE_MODEL
    opencode_client = OpenCodeClient(
        base_url,
        provider_id=provider_id,
        model_id=model_id,
        default_tools=DEFAULT_OPENCODE_TOOLS,
    )
    logger.info(
        "OpenCode client: base_url=%s provider=%s model=%s",
        opencode_client.base_url,
        opencode_client.provider_id,
        opencode_client.model_id,
    )

    # Compaction events are emitted by the history manager without a
    # user id; route them through the dispatcher which looks at the
    # current-user ContextVar.
    history_manager.set_event_sink(dispatch_event)

    session_store = SessionStore()
    logger.info("SessionStore: backing file %s", session_store.path)

    agent_manager = create_agent_manager(
        client=opencode_client,
        history_manager=history_manager,
        session_store=session_store,
    )
    logger.info("scufris runtime ready")
    return Runtime(
        config=config,
        history_manager=history_manager,
        agent_manager=agent_manager,
        opencode_client=opencode_client,
        session_store=session_store,
    )

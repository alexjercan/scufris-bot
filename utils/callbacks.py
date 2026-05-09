"""Callback handlers for the Scufris Bot agent.

The :class:`ToolCallbackHandler` below produces depth-aware traces of
agent / sub-agent / tool / LLM activity. It's used by both the Telegram
bot (where it can also drive a transport for typing actions) and the
local CLI (where the transport is omitted).
"""

from __future__ import annotations

import logging
import resource
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.outputs import LLMResult
from telegram import Update

from .logging import truncate_log
from .telegram import TelegramTransport


@dataclass
class _RunInfo:
    """Per-run state tracked by the callback handler."""

    kind: str  # "tool" | "llm" | "chain"
    name: str
    start: float
    depth: int
    parent_run_id: Optional[UUID] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ThinkingEvent:
    """A user-visible "thinking" event emitted by the callback handler.

    The CLI renders these as dim chat-style messages above the final
    assistant reply. The Telegram bot ignores them by default.
    """

    kind: Literal["text", "tool_call", "tool_result"]
    source: str  # e.g. "main", "knowledge_agent"
    text: str  # message to display
    depth: int  # nesting level (for indentation/styling)


# Type alias for the on_thinking callback.
ThinkingCallback = Callable[[ThinkingEvent], None]


def _peak_rss_kb() -> int:
    """Return peak resident set size in KB.

    On Linux ``ru_maxrss`` is in kilobytes; on macOS it's in bytes. We
    normalise to KB so the log line is consistent.
    """
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Heuristic: if the value looks like bytes (>10MB-ish for any process
    # would be > 10_000_000), assume bytes and convert.
    if rss > 10_000_000:
        rss = rss // 1024
    return rss


class ToolCallbackHandler(BaseCallbackHandler):
    """Depth-aware callback handler for logging agent activity.

    Tracks every run by its ``run_id`` so nested / concurrent calls are
    timed and indented correctly. Renders log messages with Rich markup
    for nicer terminal output (the project already wires a
    ``RichHandler``).
    """

    def __init__(
        self,
        telegram_transport: Optional[TelegramTransport] = None,
        update: Optional[Update] = None,
        on_thinking: Optional[ThinkingCallback] = None,
    ):
        super().__init__()
        self.telegram_transport = telegram_transport
        self.update = update
        self.on_thinking = on_thinking
        self.logger = logging.getLogger("scufris-bot.agent.tools")

        # run_id -> _RunInfo
        self._runs: Dict[UUID, _RunInfo] = {}

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def set_update(self, update: Optional[Update]) -> None:
        self.update = update

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _depth_for(self, parent_run_id: Optional[UUID]) -> int:
        if parent_run_id is None:
            return 0
        parent = self._runs.get(parent_run_id)
        if parent is None:
            return 0
        return parent.depth + 1

    def _prefix(self, depth: int) -> str:
        if depth == 0:
            return ""
        return "  " * (depth - 1) + "└─ "

    def _register(
        self,
        run_id: UUID,
        parent_run_id: Optional[UUID],
        kind: str,
        name: str,
    ) -> _RunInfo:
        info = _RunInfo(
            kind=kind,
            name=name,
            start=time.time(),
            depth=self._depth_for(parent_run_id),
            parent_run_id=parent_run_id,
        )
        self._runs[run_id] = info
        return info

    def _pop(self, run_id: UUID) -> Optional[_RunInfo]:
        return self._runs.pop(run_id, None)

    def _enclosing_tool_name(self, parent_run_id: Optional[UUID]) -> str:
        """Walk up the parent chain to find the nearest enclosing tool.

        Returns the tool's name (e.g. "knowledge_agent") or "main" if no
        tool ancestor exists. Used to label thinking events so the user
        knows which agent is talking.
        """
        rid = parent_run_id
        # Cap the walk to avoid pathological loops.
        for _ in range(32):
            if rid is None:
                return "main"
            info = self._runs.get(rid)
            if info is None:
                return "main"
            if info.kind == "tool":
                return info.name
            rid = info.parent_run_id
        return "main"

    def _emit(self, event: ThinkingEvent) -> None:
        """Send a thinking event to the optional listener (best-effort)."""
        if self.on_thinking is None:
            return
        try:
            self.on_thinking(event)
        except Exception:  # pragma: no cover — never break the agent
            self.logger.exception("on_thinking callback raised")

    def _maybe_log_memory(self, info: _RunInfo) -> None:
        """Log peak RSS once per top-level run."""
        if info.depth != 0:
            return
        try:
            rss_kb = _peak_rss_kb()
            self.logger.debug(f"  peak RSS: {rss_kb / 1024:.1f} MB")
        except Exception:  # pragma: no cover — never fail logging
            pass

    # ------------------------------------------------------------------
    # Tool callbacks
    # ------------------------------------------------------------------

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        name = (serialized or {}).get("name", "unknown")
        info = self._register(run_id, parent_run_id, "tool", name)
        prefix = self._prefix(info.depth)

        self.logger.info(
            f"{prefix}[bold cyan]tool[/bold cyan] [bold]{name}[/bold] "
            f"start | in={len(input_str)}c"
        )
        self.logger.debug(f"{prefix}  input: {truncate_log(input_str, 500)}")

        # Surface a short "calling X" line to the user-facing channel.
        preview = truncate_log(input_str.strip(), 80)
        self._emit(
            ThinkingEvent(
                kind="tool_call",
                source=self._enclosing_tool_name(parent_run_id),
                text=f"{name}({preview})" if preview else name,
                depth=info.depth,
            )
        )

    def on_tool_end(
        self,
        output: ToolMessage,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        info = self._pop(run_id)
        if info is None:
            # Unknown run — log minimally and bail.
            self.logger.debug(f"tool end for unknown run_id={run_id}")
            return

        duration = time.time() - info.start
        prefix = self._prefix(info.depth)
        output_content = (
            str(output.content) if hasattr(output, "content") else str(output)
        )
        status = getattr(output, "status", "ok")

        self.logger.info(
            f"{prefix}[bold cyan]tool[/bold cyan] [bold]{info.name}[/bold] "
            f"done | [yellow]{duration:.2f}s[/yellow] | status={status} | "
            f"out={len(output_content)}c"
        )
        self.logger.debug(f"{prefix}  output: {truncate_log(output_content, 500)}")
        self._maybe_log_memory(info)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        info = self._pop(run_id)
        duration = time.time() - info.start if info else 0.0
        depth = info.depth if info else self._depth_for(parent_run_id)
        name = info.name if info else "unknown"
        prefix = self._prefix(depth)
        self.logger.error(
            f"{prefix}[bold red]tool[/bold red] [bold]{name}[/bold] "
            f"failed | [yellow]{duration:.2f}s[/yellow] | {error}"
        )

    # ------------------------------------------------------------------
    # LLM / chat-model callbacks
    # ------------------------------------------------------------------

    def _llm_name(self, serialized: Optional[Dict[str, Any]]) -> str:
        if not serialized:
            return "llm"
        # langchain serialized payload usually has id=[..., "ChatOllama"]
        ids = serialized.get("id")
        if isinstance(ids, list) and ids:
            return str(ids[-1])
        return serialized.get("name", "llm")

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        name = self._llm_name(serialized)
        info = self._register(run_id, parent_run_id, "llm", name)
        prefix = self._prefix(info.depth)
        self.logger.debug(
            f"{prefix}[magenta]llm[/magenta] {name} start | prompts={len(prompts)}"
        )

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        name = self._llm_name(serialized)
        info = self._register(run_id, parent_run_id, "llm", name)
        prefix = self._prefix(info.depth)
        msg_count = sum(len(batch) for batch in messages)
        self.logger.debug(
            f"{prefix}[magenta]llm[/magenta] {name} start | messages={msg_count}"
        )

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        info = self._pop(run_id)
        if info is None:
            return
        duration = time.time() - info.start
        prefix = self._prefix(info.depth)

        # Try to extract token usage from common locations.
        tokens_str = ""
        usage: Dict[str, Any] = {}
        if response.llm_output:
            usage = (
                response.llm_output.get("token_usage")
                or response.llm_output.get("usage")
                or {}
            )
        # Modern AIMessage carries usage_metadata
        if not usage:
            for gen_list in response.generations:
                for gen in gen_list:
                    msg = getattr(gen, "message", None)
                    meta = getattr(msg, "usage_metadata", None) if msg else None
                    if meta:
                        usage = dict(meta)
                        break
                if usage:
                    break
        if usage:
            parts = []
            for key in ("input_tokens", "prompt_tokens"):
                if key in usage:
                    parts.append(f"in={usage[key]}")
                    break
            for key in ("output_tokens", "completion_tokens"):
                if key in usage:
                    parts.append(f"out={usage[key]}")
                    break
            if "total_tokens" in usage:
                parts.append(f"total={usage['total_tokens']}")
            if parts:
                tokens_str = " | tokens " + " ".join(parts)

        self.logger.debug(
            f"{prefix}[magenta]llm[/magenta] {info.name} done | "
            f"[yellow]{duration:.2f}s[/yellow]{tokens_str}"
        )

        # Extract the model's natural-language reasoning (if any) and
        # surface it as a "thinking" text event. We pull from the first
        # generation's AIMessage and look at both .content and the
        # reasoning_content extension that Ollama emits when reasoning
        # mode is on.
        thinking_text = self._extract_thinking_text(response)
        if thinking_text:
            # The LLM run we just popped was for THIS source — its parent
            # tool (if any) is the sub-agent the model belongs to.
            source = self._enclosing_tool_name(info.parent_run_id)
            self._emit(
                ThinkingEvent(
                    kind="text",
                    source=source,
                    text=thinking_text,
                    depth=info.depth,
                )
            )

    @staticmethod
    def _extract_thinking_text(response: LLMResult) -> str:
        """Pull the model's reasoning text from an LLMResult, if any.

        Looks at:
          - ``AIMessage.content`` (string or list of parts)
          - ``AIMessage.additional_kwargs['reasoning_content']`` (Ollama)

        Returns an empty string if there's nothing useful to show. We
        deliberately keep this generous about what counts as "text" —
        the CLI is the one that decides whether to render it.
        """
        for gen_list in response.generations:
            for gen in gen_list:
                msg = getattr(gen, "message", None)
                if msg is None:
                    continue

                pieces: List[str] = []

                # 1. Reasoning content (Ollama, when reasoning=True)
                add_kwargs = getattr(msg, "additional_kwargs", None) or {}
                reasoning = add_kwargs.get("reasoning_content") or add_kwargs.get(
                    "reasoning"
                )
                if isinstance(reasoning, str) and reasoning.strip():
                    pieces.append(reasoning.strip())

                # 2. Regular content
                content = getattr(msg, "content", None)
                if isinstance(content, str) and content.strip():
                    pieces.append(content.strip())
                elif isinstance(content, list):
                    # Some providers return a list of {type, text} parts.
                    for part in content:
                        if isinstance(part, dict):
                            text = part.get("text") or part.get("reasoning")
                            if isinstance(text, str) and text.strip():
                                pieces.append(text.strip())
                        elif isinstance(part, str) and part.strip():
                            pieces.append(part.strip())

                if pieces:
                    return "\n".join(pieces)
        return ""

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        info = self._pop(run_id)
        duration = time.time() - info.start if info else 0.0
        depth = info.depth if info else self._depth_for(parent_run_id)
        prefix = self._prefix(depth)
        self.logger.error(
            f"{prefix}[bold red]llm[/bold red] failed | "
            f"[yellow]{duration:.2f}s[/yellow] | {error}"
        )

    # ------------------------------------------------------------------
    # Chain callbacks (low signal — DEBUG only, named chains only)
    # ------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        name = (serialized or {}).get("name") or ""
        # Skip the unnamed wrapper chains LangChain emits for every step —
        # they swamp the log without adding signal.
        if not name or name in {"RunnableSequence", "RunnableLambda"}:
            return
        info = self._register(run_id, parent_run_id, "chain", name)
        prefix = self._prefix(info.depth)
        self.logger.debug(f"{prefix}[dim]chain[/dim] {name} start")

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        info = self._pop(run_id)
        if info is None:
            return
        duration = time.time() - info.start
        prefix = self._prefix(info.depth)
        self.logger.debug(
            f"{prefix}[dim]chain[/dim] {info.name} done | "
            f"[yellow]{duration:.2f}s[/yellow]"
        )

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        info = self._pop(run_id)
        if info is None:
            self.logger.error(f"chain error: {error}")
            return
        prefix = self._prefix(info.depth)
        self.logger.error(
            f"{prefix}[bold red]chain[/bold red] {info.name} failed | {error}"
        )

    # ------------------------------------------------------------------
    # Agent callbacks (DEBUG — informational only)
    # ------------------------------------------------------------------

    def on_agent_action(
        self,
        action: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        tool_name = getattr(action, "tool", "unknown")
        tool_input = getattr(action, "tool_input", {})
        depth = self._depth_for(parent_run_id)
        prefix = self._prefix(depth)
        self.logger.debug(
            f"{prefix}[blue]agent[/blue] -> {tool_name} | "
            f"input: {truncate_log(str(tool_input), 200)}"
        )

    def on_agent_finish(
        self,
        finish: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        depth = self._depth_for(parent_run_id)
        prefix = self._prefix(depth)
        self.logger.debug(f"{prefix}[blue]agent[/blue] finished")

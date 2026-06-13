"""HTTP client for the local opencode daemon.

Step 4 of #9. Wraps a subset of opencode's HTTP API as typed async
methods. Wider coverage (sessions list, fork, abort, permissions reply,
etc.) lands in later tasks (#10–#13, #30).

Auth
----
``OPENCODE_SERVER_PASSWORD`` becomes the HTTP Basic *password*; the
*username* is empty per opencode's plugin/serve contract. When the
password is unset (loopback dev), no Authorization header is sent.

Error model
-----------
- :class:`OpencodeNetworkError` — wraps ``httpx.RequestError`` (DNS,
  connect, read timeout).
- :class:`OpencodeClientError` — opencode returned 4xx; carries the
  parsed body.
- :class:`OpencodeServerError` — opencode returned 5xx; carries the
  parsed body.
- :class:`OpencodeUnavailable` — special case for :meth:`health`: any
  failure mode (network *or* non-200 response) is collapsed into this
  one exception so callers checking liveness have a single thing to
  catch.

Lifecycle
---------
``OpencodeClient`` owns an ``httpx.AsyncClient``; instantiate once per
process (step 5 wires this into the FastAPI lifespan) and call
:meth:`close` at shutdown. The class is also an async context manager
for CLI / test use.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OpencodeError(Exception):
    """Base for every error this module raises."""


class OpencodeNetworkError(OpencodeError):
    """Transport-level failure (connect/read timeout, DNS, refused, ...)."""

    def __init__(self, message: str, *, original: BaseException | None = None) -> None:
        super().__init__(message)
        self.original = original


class OpencodeStatusError(OpencodeError):
    """Base for HTTP non-2xx responses; carries status + parsed body."""

    def __init__(
        self,
        status_code: int,
        body: Any,
        *,
        message: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(message or f"opencode returned HTTP {status_code}: {body!r}")


class OpencodeClientError(OpencodeStatusError):
    """opencode returned a 4xx — our request was malformed."""


class OpencodeServerError(OpencodeStatusError):
    """opencode returned a 5xx — opencode is broken."""


class OpencodeUnavailable(OpencodeError):
    """Cannot get a healthy response from opencode at all.

    Raised exclusively by :meth:`OpencodeClient.health`. Collapses
    network errors *and* non-200 responses so liveness checks only
    need a single ``except``.
    """


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------
#
# Models use ``extra="allow"`` so opencode can grow new fields without
# breaking us. We pin only the fields we actually consume.


class HealthResponse(BaseModel):
    """``GET /global/health`` payload."""

    healthy: bool
    version: str


class TokenUsage(BaseModel):
    """Token counts opencode reports for an assistant turn.

    Mirrors opencode's ``AssistantMessage.tokens`` schema. ``input``
    and ``output`` are the two consumers actually want; ``reasoning``,
    ``total``, and ``cache`` are passed through verbatim for callers
    (e.g. cost reporting in #28) that want them.
    """

    model_config = ConfigDict(extra="allow")
    input: int = 0
    output: int = 0
    reasoning: int = 0
    total: int | None = None


class Session(BaseModel):
    """``POST /session`` response. Only ``id`` is required for our use."""

    model_config = ConfigDict(extra="allow")
    id: str


class AssistantInfo(BaseModel):
    """The ``info`` half of ``POST /session/:id/message`` (opencode's own
    "AssistantMessage" schema — metadata for the assistant's turn)."""

    model_config = ConfigDict(extra="allow")
    id: str
    sessionID: str
    role: str
    providerID: str | None = None
    modelID: str | None = None
    tokens: TokenUsage | None = None
    cost: float | None = None


class Part(BaseModel):
    """One element of the ``parts`` array in a message response.

    Opencode emits many ``type`` values: ``text``, ``reasoning``,
    ``step-start``, ``step-finish``, ``tool-call``, ``tool-result``.
    ``text`` is hoisted as a typed field because it's the one our
    callers read most often; everything else stays in ``model_extra``.
    """

    model_config = ConfigDict(extra="allow")
    type: str
    text: str | None = None


class AssistantMessage(BaseModel):
    """Wraps opencode's ``{info, parts}`` send-message response.

    Named per task #9 step 4 spec — distinct from :class:`AssistantInfo`
    (opencode's metadata-only schema also called "AssistantMessage").
    """

    info: AssistantInfo
    parts: list[Part]

    def text(self) -> str:
        """Concatenate every ``type=='text'`` part's body.

        Skips reasoning, step markers, tool calls/results. Returns an
        empty string when the assistant produced no plain text (e.g.
        tool-only turn).
        """
        return "".join(p.text or "" for p in self.parts if p.type == "text")


class ProviderResponse(BaseModel):
    """Parsed shape of ``GET /provider``.

    Opencode returns ``{all, default, connected}``. ``all`` is a long
    array (~150 entries) of every provider it knows about; we ignore
    it. ``default`` maps each ``providerID`` to that provider's default
    ``modelID``. ``connected`` lists the providers the operator has
    auth configured for (loopback ollama always counts).
    """

    model_config = ConfigDict(extra="allow")
    default: dict[str, str]
    connected: list[str]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ModelRef(BaseModel):
    """Identifies a model for ``send_message``."""

    providerID: str
    modelID: str


class TextPartInput(BaseModel):
    """A single text part of the outbound user message."""

    type: Literal["text"] = "text"
    text: str


class SendMessageRequest(BaseModel):
    """Body for ``POST /session/{sessionID}/message``.

    Mirrors the design §9.1 shape: ``{model, agent, system, tools,
    parts}``. Fields left ``None`` are omitted from the wire payload.
    """

    parts: list[TextPartInput]
    model: ModelRef | None = None
    agent: str | None = None
    system: str | None = None
    tools: dict[str, bool] | None = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OpencodeClient:
    """Async HTTP client for the local opencode daemon."""

    def __init__(
        self,
        base_url: str,
        password: str | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        auth = httpx.BasicAuth("", password) if password else None
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            auth=auth,
        )

    # ----- lifecycle --------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying transport. Safe to call repeatedly."""
        await self._client.aclose()

    async def __aenter__(self) -> "OpencodeClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # ----- internals --------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """Send a request and translate transport / HTTP errors.

        Returns the raw response on 2xx; raises on transport failure or
        any non-2xx status. The body is parsed (JSON if possible, else
        raw text) and attached to the raised exception.
        """
        try:
            resp = await self._client.request(method, url, json=json)
        except httpx.RequestError as exc:
            raise OpencodeNetworkError(
                f"{method} {url}: {exc!r}", original=exc
            ) from exc
        if 400 <= resp.status_code < 500:
            raise OpencodeClientError(resp.status_code, self._parse_body(resp))
        if 500 <= resp.status_code < 600:
            raise OpencodeServerError(resp.status_code, self._parse_body(resp))
        return resp

    @staticmethod
    def _parse_body(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ----- typed endpoints --------------------------------------------------

    async def health(self) -> HealthResponse:
        """Probe ``GET /global/health``.

        Any failure — network or non-200 — surfaces as
        :class:`OpencodeUnavailable`. Successful 200 responses are
        parsed into :class:`HealthResponse`.
        """
        try:
            resp = await self._client.get("/global/health")
        except httpx.RequestError as exc:
            raise OpencodeUnavailable(
                f"cannot reach opencode at {self._client.base_url}: {exc!r}"
            ) from exc
        if resp.status_code != 200:
            raise OpencodeUnavailable(
                f"opencode /global/health returned HTTP {resp.status_code}"
            )
        return HealthResponse.model_validate(resp.json())

    async def create_session(
        self,
        *,
        title: str | None = None,
        agent: str | None = None,
        parent_id: str | None = None,
    ) -> Session:
        """Create a new opencode session.

        All arguments are optional; opencode picks sensible defaults.
        ``parent_id`` (camelCase ``parentID`` on the wire) creates a
        child session — see design ADR-13 (fork → new channel).
        """
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if agent is not None:
            body["agent"] = agent
        if parent_id is not None:
            body["parentID"] = parent_id
        resp = await self._request("POST", "/session", json=body)
        return Session.model_validate(resp.json())

    async def send_message(
        self,
        session_id: str,
        request: SendMessageRequest,
    ) -> AssistantMessage:
        """Send a message to a session and block until the reply arrives.

        Opencode runs the turn synchronously when called this way:
        tool calls execute, the model produces text, and the full
        ``{info, parts}`` payload comes back in the response.
        """
        payload = request.model_dump(exclude_none=True)
        resp = await self._request(
            "POST", f"/session/{session_id}/message", json=payload
        )
        return AssistantMessage.model_validate(resp.json())

    async def get_default_model(self) -> ModelRef | None:
        """Resolve a usable default ``(providerID, modelID)`` from opencode.

        Algorithm (mirrors what TUI users get when they don't pick a
        model explicitly):

        1. Fetch ``GET /provider``.
        2. Walk ``connected`` in order. For each provider id, look it
           up in ``default``; if present, return that pair.
        3. Return ``None`` if no connected provider has a default —
           i.e. opencode has nothing it can route a message to. The
           caller decides what to do (we hard-fail ``/v1/chat`` with a
           503 in this case; see step 8 of #9).

        Network errors and non-2xx responses propagate as the usual
        :class:`OpencodeNetworkError` / :class:`OpencodeServerError`
        exceptions; lifespan callers catch them and degrade gracefully.
        """
        resp = await self._request("GET", "/provider")
        parsed = ProviderResponse.model_validate(resp.json())
        for provider_id in parsed.connected:
            model_id = parsed.default.get(provider_id)
            if model_id:
                return ModelRef(providerID=provider_id, modelID=model_id)
        return None

"""Boot scufris-server and exercise POST /v1/chat/stream against live opencode.

Purpose
-------
End-to-end smoke for the SSE streaming chat endpoint landed in #11.
Mirrors :mod:`check_chat` but reads the response as a server-sent
event stream: each ``thinking`` event is printed to stderr (dim-italic
if stderr is a TTY) as it arrives, and the final reply from the
``done`` event goes to stdout. Two turns on the same channel verify
session reuse end-to-end on top of the streaming path.

Uses :class:`httpx.AsyncClient` for the POST + ``aiter_lines()`` SSE
read (per #11 step 12). The SSE parser is hand-rolled (~30 LoC,
inline in :func:`_stream_one_turn`) — mirrors the v1 client at
``feature/opencode:scufris_client/client.py:99`` and avoids pulling
in ``httpx-sse`` for one shape of stream.

How to run
----------
Start opencode first (in another terminal). It needs at least one
connected provider with a default model — locally that's ollama with
``qwen3:latest`` pulled::

    ollama serve                       # if not already running
    ollama pull qwen3:latest           # one-time
    opencode serve --port 4096

Then from the repo root::

    python examples/check_chat_stream.py

Override defaults via env vars::

    OPENCODE_URL=http://192.0.2.1:4096 \\
      OPENCODE_SERVER_PASSWORD=hunter2 \\
      python examples/check_chat_stream.py

The script pins ``SCUFRIS_USER_ID=1`` on the spawned server so
identity resolution is deterministic regardless of the host's
``config.toml`` or any pre-existing ``SCUFRIS_USER_ID`` in the
caller's environment (same pattern as :mod:`check_sessions`). The
pin matches ``DEFAULT_USER_ID`` in :mod:`scufris_server.identity`.

Expected output
---------------
For each turn:

- stderr: a section header and zero-or-more thinking lines as the
  stream produces them — rendered dim-italic when stderr is a TTY so
  the live tool-call trace stays visually distinct from the final
  reply on stdout.
- stdout: a labelled section with the final assistant reply text,
  the ``oc_session_id``, ``oc_message_id``, ``tokens``, and ``cost``
  decoded from the terminal ``done`` event.

The script asserts both turns reach a ``done`` event and that the
second turn reuses the ``oc_session_id`` from the first. Footer:
``OK``.

If ``/v1/chat/stream`` returns 503 with
``error_type=DefaultModelMissing`` before the stream is committed,
opencode has no connected provider; check ``ollama serve`` and the
provider auth in your opencode config. The script exits non-zero
with the structured ``ChatErrorBody`` printed.

If the stream emits an ``event: error`` mid-flight (post-stream-
commit opencode failure tunnelled per ``routes/chat_stream.py``),
the script prints the error payload to stderr and exits non-zero.

Requires
--------
``opencode serve`` reachable at ``OPENCODE_URL`` (default
``http://127.0.0.1:4096``) **and** at least one connected provider
with a default model. With ollama as the default, ``qwen3:latest``
is the conventional pick. ``httpx`` (already a project dep). A free
TCP port on 127.0.0.1 (auto-picked).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import httpx

# Two short, deterministic-ish prompts. They won't trigger tool calls
# (the model can answer in one token) so the ``thinking`` stream is
# dominated by ``text`` events — keeps the stderr trace readable on a
# cold model.
PROMPT_FIRST = "reply with the single word: pong"
PROMPT_SECOND = "reply with the single word: ack"

CHANNEL = {
    "surface": "example",
    "surface_id": "check_chat_stream",
    "agent": "build",
}

# Default user_id seeded by the lifespan (matches ``DEFAULT_USER_ID``
# in :mod:`scufris_server.identity`). We pin the spawned server to this
# user via the ``SCUFRIS_USER_ID`` env var (see :func:`main` below) so
# the script is independent of the host's ``config.toml`` and any
# pre-existing ``SCUFRIS_USER_ID`` in the caller's environment. Same
# rationale as :mod:`check_sessions`.
DEFAULT_USER_ID = 1

BOOT_TIMEOUT_S = 10.0
# Cold-start qwen3 can take 15s+ on the first turn; budget generously.
# The full per-turn budget also covers the event-bus subscription
# latency and the final ``done`` round-trip from the background poster.
STREAM_TIMEOUT_S = 90.0
HTTP_TIMEOUT_S = 10.0  # for the synchronous /v1/healthz pre-flight
SHUTDOWN_TIMEOUT_S = 5.0

# ANSI escape codes for dim-italic stderr rendering on a TTY. Stdlib
# only — keeps the example dep-free beyond ``httpx``. The reset code
# clears both attributes in one go (SGR 0).
ANSI_DIM_ITALIC = "\033[2;3m"
ANSI_RESET = "\033[0m"


def _pick_free_port() -> int:
    """Bind a transient socket to ``:0`` and return the kernel-assigned port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ready(url: str, timeout_s: float) -> None:
    """Block until ``url`` returns HTTP 200, or raise :class:`TimeoutError`."""
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as resp:  # noqa: S310 - http://127.0.0.1
                if resp.status == 200:
                    return
        except (URLError, ConnectionError, TimeoutError) as exc:
            last = exc
        time.sleep(0.1)
    raise TimeoutError(f"server not ready within {timeout_s}s (last: {last!r})")


def _format_thinking(payload: dict[str, Any]) -> str:
    """Compact one-line render of a thinking-event payload for stderr.

    The payload shape matches
    :class:`scufris_server.events.ThinkingEvent.to_payload` — ``kind``
    is one of ``text``, ``tool_call``, ``tool_result``, ``tool_meta``,
    ``compaction``. The render picks the most diagnostically useful
    fields per kind rather than dumping the whole dict (which would
    flood the terminal on a long turn).

    ``depth`` is rendered as two-space indentation so nested subagent
    activity (depth > 0) is visually distinct from the top-level
    agent's output. ``source`` (``scufris`` for synthesised events,
    ``opencode`` for forwarded ones) goes in a bracket prefix.
    """
    kind = payload.get("kind", "?")
    source = payload.get("source", "?")
    depth = int(payload.get("depth", 0) or 0)
    indent = "  " * depth
    text = payload.get("text", "")
    if kind == "tool_call":
        arg = payload.get("arg") or ""
        suffix = f"({arg!r})" if arg else ""
        return f"{indent}[{source}] tool_call: {text}{suffix}"
    if kind == "tool_result":
        return f"{indent}[{source}] tool_result: {text}"
    if kind == "tool_meta":
        return f"{indent}[{source}] tool_meta: {text}"
    if kind == "compaction":
        prior = payload.get("prior_turns")
        evicted = payload.get("evicted")
        return f"{indent}[{source}] compaction: prior={prior} evicted={evicted}"
    # Default: kind=="text" or unknown — show a bounded preview so a
    # long-reasoning chunk doesn't wrap a hundred terminal lines.
    preview = text if len(text) < 80 else text[:77] + "..."
    return f"{indent}[{source}] {kind}: {preview}"


def _emit_thinking(payload: dict[str, Any]) -> None:
    """Print a single thinking event to stderr (dim-italic on a TTY)."""
    line = _format_thinking(payload)
    if sys.stderr.isatty():
        line = f"{ANSI_DIM_ITALIC}{line}{ANSI_RESET}"
    print(line, file=sys.stderr, flush=True)


async def _stream_one_turn(
    client: httpx.AsyncClient,
    base: str,
    message: str,
    channel: dict[str, str],
) -> dict[str, Any]:
    """POST ``/v1/chat/stream`` and consume the SSE stream to its terminal.

    Returns the parsed ``done`` payload on success. Raises
    :class:`RuntimeError` for:

    - pre-stream HTTP failures (non-200 from ``POST`` — body is the
      structured ``ChatErrorBody`` shape under ``detail``),
    - ``event: error`` records on the stream (post-stream-commit
      opencode failures tunnelled per ``routes/chat_stream.py``),
    - the stream closing without a terminal event (server bug or
      premature disconnect).

    Each ``event: thinking`` payload is forwarded to
    :func:`_emit_thinking` as it arrives so the user sees live
    progress while the model is still generating.

    The SSE parser handles the subset of the spec we actually use:
    ``event:`` / ``data:`` lines, blank-line dispatch, and comment
    lines (``:`` prefix, used for keepalives) which are dropped.
    Multi-line ``data:`` is concatenated with newlines per spec
    (our server always emits one-line payloads, but the parser is
    lenient for robustness).
    """
    payload = {"message": message, "channel": channel}
    async with client.stream(
        "POST",
        f"{base}/v1/chat/stream",
        json=payload,
        timeout=STREAM_TIMEOUT_S,
    ) as resp:
        if resp.status_code != 200:
            # Pre-stream error: body is JSON {detail: ChatErrorBody}.
            body_bytes = await resp.aread()
            raw = body_bytes.decode("utf-8", errors="replace") or "{}"
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"raw": raw}
            rid = resp.headers.get("x-request-id")
            raise RuntimeError(
                f"POST /v1/chat/stream returned {resp.status_code} "
                f"(x-request-id={rid}): {json.dumps(body)}"
            )

        event_name = ""
        data_buf: list[str] = []
        async for raw_line in resp.aiter_lines():
            # ``aiter_lines`` strips the trailing ``\n``; we strip any
            # stray ``\r`` for robustness against CRLF intermediaries.
            line = raw_line.rstrip("\r")
            if line == "":
                # Blank line is the SSE record terminator — dispatch.
                if event_name or data_buf:
                    payload_text = "\n".join(data_buf)
                    parsed: dict[str, Any] = (
                        json.loads(payload_text) if payload_text else {}
                    )
                    if event_name == "thinking":
                        _emit_thinking(parsed)
                    elif event_name == "done":
                        return parsed
                    elif event_name == "error":
                        raise RuntimeError(
                            f"stream emitted error event: "
                            f"error_type={parsed.get('error_type')!r} "
                            f"message={parsed.get('error')!r}"
                        )
                    # Unknown event name — defensive ignore (forward-
                    # compatible with future event types added by #11
                    # follow-ups or #30 permissions).
                event_name = ""
                data_buf = []
                continue
            if line.startswith(":"):
                # Comment line (used for keepalive ``: keepalive``) —
                # drop per SSE spec.
                continue
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
                continue
            if line.startswith("data:"):
                # Spec says one leading space after ``data:`` is part
                # of the prefix, not the payload — strip only that one
                # space (lstrip(" ") would also strip embedded spaces
                # of a multi-byte UTF-8 char, which is fine since we
                # only strip leading runs and our payloads never start
                # with whitespace anyway).
                data_buf.append(line[len("data:") :].lstrip(" "))
                continue
            # Unknown field name — ignore per SSE spec.

        # Stream closed without a terminal event. Either a server bug
        # (drop both _PostDone and _PostError sentinels) or the upstream
        # opencode connection died between events and the handler's
        # ``finally`` ran before any sentinel landed on the queue.
        raise RuntimeError(
            "stream closed without a terminal event "
            "(server bug? premature client disconnect?)"
        )


def _show_done(label: str, done: dict[str, Any]) -> None:
    """Print the final ``done`` payload as a labelled stdout section."""
    print(f"=== {label} ===")
    print(f"oc_session_id:  {done.get('oc_session_id')}")
    print(f"oc_message_id:  {done.get('oc_message_id')}")
    print(f"tokens:         {done.get('tokens')}")
    print(f"cost:           {done.get('cost')}")
    print("message:")
    print(done.get("message", ""))
    print()


async def _run(base: str) -> int:
    """Pre-flight ``/v1/healthz``, then stream two turns on the same channel.

    Returns 0 if both turns reach ``done`` and the second reuses the
    first's ``oc_session_id``. Returns 1 on a degraded opencode
    pre-flight; any other failure raises (caught by :func:`main`'s
    top-level handler).
    """
    async with httpx.AsyncClient() as client:
        # Pre-flight: opencode reachable from the server's POV?
        # ``/v1/healthz`` aggregates the lifespan probe + a fresh
        # ``/global/health`` round-trip per request.
        resp = await client.get(f"{base}/v1/healthz", timeout=HTTP_TIMEOUT_S)
        print("=== GET /v1/healthz (pre-flight) ===")
        print(f"status:  {resp.status_code}")
        print(f"body:    {resp.text}")
        print()
        if not resp.json().get("opencode", {}).get("healthy"):
            print(
                "ERROR: opencode is not reachable from scufris-server. "
                "Start `opencode serve --port 4096` and rerun."
            )
            return 1

        # Turn 1: new channel, expect a fresh opencode session.
        print("=== POST /v1/chat/stream (turn 1, new) ===", file=sys.stderr)
        done_a = await _stream_one_turn(client, base, PROMPT_FIRST, CHANNEL)
        _show_done("done (turn 1)", done_a)
        session_a = done_a["oc_session_id"]

        # Turn 2: same channel, must reuse session.
        print("=== POST /v1/chat/stream (turn 2, reuse) ===", file=sys.stderr)
        done_b = await _stream_one_turn(client, base, PROMPT_SECOND, CHANNEL)
        _show_done("done (turn 2)", done_b)
        assert done_b["oc_session_id"] == session_a, (
            f"same channel must reuse session: "
            f"{done_b['oc_session_id']!r} != {session_a!r}"
        )
    return 0


def main() -> int:
    """Spawn the server in a temp state dir, run the smoke, tear down."""
    host = "127.0.0.1"
    port = _pick_free_port()
    base = f"http://{host}:{port}"

    with tempfile.TemporaryDirectory(prefix="scufris-check-chat-stream-") as td:
        state_dir = Path(td)
        env = {
            **os.environ,
            "SCUFRIS_STATE_DIR": str(state_dir),
            "SCUFRIS_BIND": host,
            "SCUFRIS_PORT": str(port),
            # Pin the spawned server to user 1 unconditionally. See
            # the module docstring + ``check_sessions.py`` for the
            # full rationale; in short, surface=``example`` matches no
            # TOML row, so without the pin we'd fall through to
            # whatever ``SCUFRIS_USER_ID`` happens to be set in the
            # caller's environment.
            "SCUFRIS_USER_ID": str(DEFAULT_USER_ID),
        }
        cmd = [sys.executable, "-m", "scufris_server"]
        opencode_url = env.get("OPENCODE_URL", "http://127.0.0.1:4096")
        print(f"spawning:    {' '.join(cmd)}")
        print(f"state_dir:   {state_dir}")
        print(f"bind:        {host}:{port}")
        print(f"opencode:    {opencode_url}")
        print()

        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        ok = False
        out = ""
        try:
            _wait_ready(f"{base}/openapi.json", timeout_s=BOOT_TIMEOUT_S)
            rc = asyncio.run(_run(base))
            ok = rc == 0
        except (AssertionError, TimeoutError, RuntimeError, URLError) as exc:
            print(f"ERROR: {exc!r}")
        finally:
            print("shutting down")
            proc.terminate()
            try:
                out, _ = proc.communicate(timeout=SHUTDOWN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
            if not ok and out:
                print()
                print("subprocess output:")
                print(out, end="" if out.endswith("\n") else "\n")

    print()
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

# PLAN: Step 3 — SDK chat + state

## Status

**Gap identified:** `client.py` (~868 LoC) is complete with all step 3 code, but `test_scufris_client.py` only has the step 2 tests (19 test functions, ~18 cases, ~388 lines). Step 3's tests are not yet written.

## What's already done (in `client.py`)

| Feature | Location | Confirmed |
|---------|----------|-----------|
| `ThinkingEvent` dataclass | `client.py:126-179` | Yes, 9 fields wire-compat |
| `StreamEvent` dataclass | `client.py:182-234` | Yes, discriminator with 9 fields |
| `_thinking_from_payload()` | `client.py:237-262` | Yes |
| `_dispatch()` | `client.py:265-332` | Yes — handles thinking, done, error, unknown |
| `_parse_sse_stream()` | `client.py:335-382` | Yes — full SSE parser with comment drops |
| `chat_stream()` | `client.py:566-666` | Yes — POST /v1/chat/stream with SSE iterate |
| `sessions()` | `client.py:668-696` | Yes — GET /v1/sessions, uses `_json_list()` |
| `clear_session()` | `client.py:698-722` | Yes — POST /v1/sessions/{id}/clear |
| `clear()` | `client.py:724-747` | Yes — POST /v1/clear |
| `_json_list()` | `client.py:798-842` | Yes — list-returning sibling of `_json()` |
| `__init__.py` exports | lines 40-41 | Yes — `StreamEvent`, `ThinkingEvent` |

## What needs to be done

### 3a. Chat stream tests (`_parse_sse_stream` + `chat_stream`)

**Approach:** Use `httpx.MockTransport` + `httpx.Response` with streaming body. The `_parse_sse_stream` helper is already tested implicitly by `chat_stream`, so we test it through `client.chat_stream()`.

**Test cases (target: ~6 tests):**

1. `test_chat_stream_happy_path` — Server returns SSE stream: `thinking` (text_delta) + `thinking` (text_delta) + `done`. Assert we get 3 events back with correct data. Verify request body shape (message + channel with surface/surface_id/agent). Verify `Accept: text/event-stream` header.

2. `test_chat_stream_error_terminal` — Server returns `thinking` + `error`. Assert second event has `kind="error"` with correct `error`/`error_type`.

3. `test_chat_stream_pre_stream_503` — Server returns 503 before SSE body starts. Should raise `ScufrisServerError` with structured body from response (not a parse error).

4. `test_chat_stream_pre_stream_connect_error` — Transport raises `ConnectError`. Should raise `ScufrisConnectionError`.

5. `test_chat_stream_keepalive_comments_dropped` — Server emits `: keepalive` lines interspersed. Assert they are silently ignored (no extra events).

6. `test_chat_stream_unknown_event_synthesized` — Server emits an event with an unexpected name. `_dispatch` returns `StreamEvent(kind="error", error="unexpected SSE event: ...", error_type="UnknownSSEEvent")`.

**Key pattern:** Build SSE payload strings with `httpx.Response` and `httpx.StreamingStream` or use `stream=True` with raw bytes.

Example SSE fixture for `chat_stream`:
```python
sse_body = b"""\nevent: thinking
data: {"kind": "text", "source": "scufris", "text": "Hello", "depth": 0}

event: thinking
data: {"kind": "text", "source": "scufris", "text": " world", "depth": 0}

event: done
data: {"message": "Hello world", "oc_session_id": "ses_abc123", "oc_message_id": "msg_456", "tokens": {"input": 10, "output": 5}, "cost": 0.01}

"""
```

### 3b. Sessions tests

**Test cases (target: ~3 tests):**

7. `test_sessions_empty` — Server returns `200 []`. Assert empty list. Verify `user_id` query param.

8. `test_sessions_populated` — Server returns `200` with list of channel objects. Assert list length and field presence.

9. `test_sessions_user_id_query_param` — Verify request includes `?user_id=N` query string.

### 3c. Clear tests

**Test cases (target: ~3 tests):**

10. `test_clear_session` — Server returns `200 {"cleared": true}`. Assert response.

11. `test_clear` — Server returns `200 {"count": 3}`. Assert response. Verify request body `{"user_id": N}`.

12. `test_clear_zero_count` — Server returns `200 {"count": 0}`. Should be treated as success (not an error).

### 3d. SSE parsing edge cases (direct `_parse_sse_stream` tests)

**Test cases (target: ~3 tests):**

13. `test_parse_sse_multiline_data` — Data spans multiple lines (`data:` + ` data: `). Should be joined with `\n`.

14. `test_parse_sse_trailing_event_no_final_blank` — Last event without trailing blank line. Should still be dispatched (defensive).

15. `test_parse_sse_unknown_sse_field_dropped` — Lines with `id:`, `retry:` etc. are dropped per SSE spec.

### 3e. Error mapping for streaming

**Test cases (target: ~2 tests):**

16. `test_chat_stream_malformed_json_in_event` — SSE event with invalid JSON data. Should raise `ScufrisServerError`.

17. `test_chat_stream_missing_required_thinking_field` — `thinking` event missing `kind` or `source`. Should raise `ScufrisServerError`.

## Total target: ~14-15 new test functions (~10+ additional parametrize cases = ~20+ test cases)

Current file: 19 test functions (~227 LoC of test code).
After step 3: ~33-34 test functions (~370+ LoC of test code).

## Implementation approach

### Test helper: SSE response builder

Create a helper function to build streaming SSE responses for mock transport:

```python
def _sse_response(sse_data: str, status: int = 200) -> httpx.Response:
    """Build a StreamingHTTPResponse with SSE data."""
    return httpx.Response(
        status_code=status,
        content=sse_data.encode(),
        headers={"Content-Type": "text/event-stream"},
    )
```

For streaming, use `httpx.Response(streaming_stream=...)` or create a custom streaming transport.

### File structure addition

Add tests at the end of `test_scufris_client.py` in sections:

```
# ---------------------------------------------------------------------------
# /v1/chat/stream
# ---------------------------------------------------------------------------

# (tests 1-6, 16-17)

# ---------------------------------------------------------------------------
# /v1/sessions
# ---------------------------------------------------------------------------

# (tests 7-9)

# ---------------------------------------------------------------------------
# /v1/sessions/{id}/clear
# ---------------------------------------------------------------------------

# (tests 10)

# ---------------------------------------------------------------------------
# /v1/clear
# ---------------------------------------------------------------------------

# (tests 11-12)

# ---------------------------------------------------------------------------
# SSE parsing edge cases
# ---------------------------------------------------------------------------

# (tests 13-15)
```

## Gate checklist (after implementation)

- [x] `ruff check tests/unit/test_scufris_client.py` clean
- [x] `ruff format --check tests/unit/test_scufris_client.py` clean
- [x] `mypy --strict scufris_client` clean (tests are default mypy mode, not strict)
- [x] `pytest tests/unit` — all 292 existing + ~14 new tests pass
- [x] Verify `StreamEvent` and `ThinkingEvent` are imported in test imports (already present in step 2)

## Notes

- `StreamEvent` and `ThinkingEvent` are already imported at lines 43-51 of the test file — no import changes needed.
- `_parse_sse_stream` is a module-level function (not on the client), so it needs separate direct tests (tests 13-15).
- The existing `_make_client` and `_run` helpers (lines 59-79) work for all new tests — no changes needed there.
- For streaming tests, use `httpx.Response` with the `content` parameter and a streaming approach. The exact mechanism for mocking SSE in tests will need to be verified during implementation.

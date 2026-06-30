# Permissions UX bridge: opencode permission.asked → Telegram inline buttons

- STATUS: OPEN
- PRIORITY: 73
- TAGS: telegram,permissions,opencode

## Goal

Bridge opencode's permission flow into the user-facing clients (Telegram first,
CLI second). When opencode emits a `permission.asked` event via the SSE stream,
surface it to the corresponding chat as an interactive prompt (inline buttons
for Telegram, terminal prompt for CLI), then relay the user's choice back to
the server.

## Description

Currently, when an agent attempts a gated tool call, opencode emits a
permission request. Without a UX bridge, the agent loop stalls until the
opencode-side timeout occurs. This task implements the "bridge" that allows
users to respond to these requests in real-time through their preferred
interface.

The flow is:
1. **Opencode** emits `permission.updated` (mapped to `ThinkingEvent(kind="tool_meta")`) via the SSE stream.
2. **Client (Telegram/CLI)** catches this event.
3. **Client** presents a prompt to the user:
   - **Telegram**: An inline keyboard message with options: `Allow`, `Allow Once`, `Deny`.
   - **CLI**: A standard terminal input prompt.
4. **Client** sends the response to `POST /v1/permissions/{perm_id}/reply` using the `session_id` and `perm_id` extracted from the event.

This is a hard prerequisite before any destructive tools (`bash`, `edit`, `write`, etc.) can be safely enabled in production.

## Acceptance Criteria

- [ ] **SSE Mapping Update**: Update `scufris_server.event_mapping.map_opencode_event` to include the `perm_id` in the `arg` field of the `ThinkingEvent` when `etype == "permission.updated"`.
- [ ] **Telegram Implementation**:
    - [ ] The Telegram bot detects `tool_meta` permission events in the `chat_stream`.
    - [ ] It posts an inline keyboard message with `Allow`, `Allow Once`, and `Deny` buttons.
    - [ ] Button `callback_data` includes the `perm_id` (e.g., `perm:allow:<perm_id>`).
    - [ ] Clicking a button triggers a `POST /v1/permissions/{perm_id}/reply` request via the `ScufrisClient`.
- [ ] **CLI Implementation**:
    - [ ] The CLI client detects `tool_meta` permission events in the stream.
    - [ ] It pauses the stream rendering and prompts the user in the terminal.
    - [ ] User input is mapped to the correct API call.
- [ ] **Error Handling**: Handle cases where the permission expires or the session is lost before the user responds.

## Open Questions

- **Event Payload**: Does the opencode `permission.updated` event provide the
  `perm_id` and the list of available options in its `properties`? (Assuming
  `id` is the `perm_id`).
- **Option Set**: Are "Allow", "Allow Once", and "Deny" the only possible
  options, or should the UI be dynamic based on the event payload?
- **CLI UX**: Should the CLI prompt be an interactive `input()` call that
  blocks the async loop, or should it use a more sophisticated non-blocking
  approach?
- **Session Context**: How does the server ensure the `perm_id` is correctly
  associated with the active session when the reply arrives? (Presumably via
  the `session_id` in the URL).

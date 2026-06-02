# CLI UX polish: slash-command autocomplete, persistent multi-line history, status pane

- STATUS: OPEN
- PRIORITY: 0
- TAGS: cli,ux

## Goal

Make `scufris-cli` feel more like a real shell: tab-complete the slash
commands, remember multi-line inputs across sessions, and give the
user an always-visible status line showing connection state, model,
and whether a request is in flight.

## Scope

### In
- Tab-completion for `/help`, `/clear`, `/stats`, `/multiline`,
  `/thinking`, `/exit`, `/quit` (and any others). Probably switch
  the line editor from raw `readline` to `prompt_toolkit`, which gives
  completion + multi-line + key bindings out of the box.
- Persist multi-line block inputs to history as a single entry so
  arrow-up replays the whole thing, not the last `.` terminator.
- A bottom status line / toolbar showing:
  - server URL + connection state (connected / reconnecting / down)
  - model name (from `/v1/version`)
  - "thinking…" indicator while a request is streaming
- Graceful fallback if `prompt_toolkit` is somehow unavailable: keep
  the current `readline` path behind a flag so the install footprint
  stays small in environments that don't want it.
- Keep current behaviour of `--quiet`, `--short-thinking`, the
  history file location (`~/.scufris_cli_history`), and Ctrl-D exit.

### Out
- A full TUI (split panes, etc.) — that's a separate task if ever.
- Server-side changes; this is purely client UX.

## Acceptance criteria

- Typing `/c<TAB>` cycles through `/clear`. Slash-commands list
  surfaces in a popup or completion menu.
- After `/multiline … .`, pressing arrow-up restores the entire
  block as one editable buffer.
- Status line updates live: shows "thinking" while streaming, clears
  when the response is complete, flips to "disconnected" if the
  server is unreachable mid-session.
- All existing CLI tests still pass; new tests cover the autocomplete
  and history-persistence bits with a mocked editor.
- README's CLI section gets a short "shortcuts" subsection.

## Notes

- `prompt_toolkit` is already in the langchain dep tree transitively,
  so adding it as a direct dep is essentially free in the closure.
- Keep latency tight: the status line redraw must not block keypresses.

## References

- `cli.py`, `scufris_client/client.py`.
- Closed UX work: `tasks/20260509-150002/TASK.md`,
  `tasks/20260509-143932/TASK.md`.

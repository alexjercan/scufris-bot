# Plan scufris-v2: opencode-based rewrite backlog

- STATUS: OPEN
- PRIORITY: 100
- TAGS: planning,opencode,rewrite

## Goal

Scufris (the LangChain-based multi-agent bot/server) gets a clean-slate
sibling project, **scufris-v2**, built around `opencode` as the main
agent runtime instead of a hand-rolled LangChain agent hierarchy.

This task is the **planning umbrella** — it doesn't implement anything.
Its job is to:

1. Capture the full backlog of work needed to reach parity with (and
   eventually replace) the current scufris-bot, adapted to an
   opencode-centric architecture.
2. Spin off one `tatr` task per backlog item below, with the priorities
   and tags as given, so each can be picked up, fleshed out (full
   `TASK.md` with scope/acceptance criteria), and worked independently.

## Why opencode as the core

The current project hand-builds an agent hierarchy (main agent +
4 sub-agents) on LangChain + Ollama, plus all the supporting
infrastructure: per-agent memory, compaction, tool callbacks, a
custom HTTP server, CLI/Telegram clients, Nix packaging.

`opencode` already provides a capable agent runtime (sessions, tool
use, multi-model routing). Rebuilding scufris on top of it should let
us drop a large fraction of the custom agent-loop and
callback-plumbing code, and focus our effort on:

- the domain-specific tools (journal, weather, etc.)
- the user-facing surfaces (CLI, Telegram)
- the deployment story (Nix, systemd, CI)

## Scope of this task

### In
- Reviewing the backlog below for completeness against what
  scufris-bot v1 currently does (see references).
- Filing one `tatr` task per backlog row, using the given
  `title, priority, tags`, with a short description (1–3 sentences)
  expanding on the one-liner below. Full design/acceptance-criteria
  detail is **not** required at filing time — that's the job of each
  spun-off task when it's picked up.
- Noting any obvious sequencing/dependency constraints between tasks
  (e.g. "design doc before everything else", "tools before clients")
  as comments on the filed tasks or in this doc's Notes section.

### Out
- Implementing any of the backlog items.
- Writing full `TASK.md`s for each item (each gets fleshed out when
  picked up, same as the v1 project's workflow).
- Deciding the final opencode integration shape — that's the output of
  backlog item #1 (the opencode capabilities spike).

## Backlog

Format: `title, priority, tags` — one-line note follows each.

### Foundations / design

1. `Spike: opencode as agent runtime — sessions, tools, plugin API, config model, 95, spike,design,opencode`
   Survey what opencode actually exposes (session create/list, tool
   registration, streaming events, multi-agent/model config) before
   committing to an architecture. Blocks almost everything else.

2. `Design doc: scufris-v2 architecture around opencode, 90, design,architecture`
   Replaces the v1 multi-agent design doc. Defines how "Scufris" maps
   onto opencode sessions/agents, where the HTTP layer sits, and what
   we own vs. what opencode owns.

3. `Spike: per-(user, agent) session/context model on top of opencode, 70, spike,memory,opencode`
   Figure out the opencode equivalent of v1's per-(user, agent) history
   + compaction — does opencode persist sessions, for how long, and can
   we reuse that instead of our own SQLite store?

### Tools / integrations

4. `Port journal tools (today/daily/macros) as opencode tools, 85, tools,journal,opencode`
   Wrap the existing `today`/`daily`/`macros` CLIs as opencode-compatible
   tool definitions.

5. `Port weather/web_search/calculator/datetime tools to opencode, 75, tools,opencode`
   Migrate the existing `@tool`-based implementations to opencode's tool
   schema.

6. `OS agent tools (file management, system monitor) for opencode, 40, tools,os,opencode`
   Carry over the planned utilities→os-agent split (file management +
   system monitor) as opencode tools.

7. `Calendar (CalDAV/ICS) tools for opencode, 30, tools,journal,calendar`
   Same scope as the v1 calendar-integration task, ported to opencode
   tools.

8. `Reminder / NL date parsing tools for opencode, 30, tools,journal,nlp`
   Same scope as the v1 NL-reminders task, ported to opencode tools.

### Server / daemon

9. `scufris-server v2: HTTP daemon wrapping opencode serve, 90, server,deploy,opencode`
   Single long-running process that owns opencode session lifecycle and
   exposes our own `/v1/*` API (chat, stream, stats, clear) on top of
   `opencode serve`.

10. `Per-user opencode session management (create/resume/expire), 85, server,sessions,memory`
    Map our `user_id` to opencode session IDs; handle creation, reuse,
    and cleanup/expiry.

11. `SSE streaming of opencode "thinking" events, 75, server,observability`
    Bridge opencode's tool-call/reasoning stream into the same
    `ThinkingEvent`-style SSE the v1 CLI already knows how to render.

12. `Identity layer + XDG user config (config.toml), 75, identity,config`
    Carry over the v1 identity/config design (per-user TOML, surface
    bindings for CLI/Telegram).

13. `/stats and /clear endpoints + per-user telemetry, 50, server,observability`
    Port the stats/clear UX, adapted to opencode session data.

14. `Auth: bearer token for non-localhost binds, 40, server,security`
    Same bearer-token story as v1 for non-loopback deployments.

### Clients

15. `scufris-cli v2: REPL client over HTTP, 80, cli`
    REPL with slash commands, streaming thinking trace, and history,
    talking to the new server.

16. `Telegram bot v2: client over HTTP, 75, telegram,bot`
    Telegram adapter for the new server, mirroring CLI capabilities
    (typing indicators, edit-in-place streaming).

17. `CLI UX polish: autocomplete, multiline history, status pane, 20, cli,ux`
    Same as the deferred v1 polish task — tab-complete slash commands,
    persistent multi-line history, status line.

### Memory / persistence

18. `Persistent conversation/session store (SQLite), 60, memory,persistence`
    If opencode doesn't persist sessions long-term, build our own store
    + export (md/json), as in v1.

19. `User facts store + remember/forget tools, 40, memory,facts`
    Durable per-user facts layer, adapted for opencode tool-calling.

20. `History compaction strategy for opencode context windows, 35, memory,compaction`
    Summary + facts layers if opencode's own context management isn't
    sufficient on its own.

### Deployment / ops

21. `Nix flake: package scufris-server-v2 and scufris-cli-v2 (uv2nix, flake-parts), 70, nix,deploy`
    Same shape as the v1 flake task, including overlays for any missing
    opencode-related deps.

22. `NixOS module + hardened systemd unit, 60, nix,systemd,deploy`
    Port the v1 module (DynamicUser, hardening, VM test) to the new
    server.

23. `Home Manager module, 55, nix,home-manager,deploy`
    Port the v1 Home Manager module (cli + optional user service).

24. `CI: GitHub Actions for ruff/pytest/mypy/nix flake check, 50, ci`
    Same QA gate as v1.

25. `Secrets/config injection guide (env-file / sops-nix / agenix), 30, deploy,security,docs`
    Port the v1 secrets doc, adjusted for opencode's own config/secrets
    if any.

### Quality / migration

26. `Test harness with mocked opencode client, 45, testing,opencode`
    Equivalent of v1's "mock at the SDK boundary" approach, but for
    opencode's API.

27. `Decommission/retire old LangChain-based scufris-bot, 15, chore,migration`
    Once v2 reaches parity, document and remove the old stack.

## Acceptance criteria

- [ ] Backlog above reviewed against v1 scope for gaps (anything from
      scufris-bot's feature set that's missing here is added as a new
      row before filing).
- [ ] One `tatr` task filed per backlog row above, with the given
      title/priority/tags and a short (1–3 sentence) description.
- [ ] Sequencing notes captured (at minimum: #1 and #2 block most
      other tasks; tools (#4–8) should land before or alongside the
      clients that exercise them; #21–24 can proceed in parallel with
      the server/client work once #1/#2 settle the package layout).
- [ ] Each filed task cross-references this planning doc (e.g.
      "Spun off from scufris-v2 planning doc").

## Notes

- This is intentionally a near 1:1 re-scoping of the v1 project's
  completed + open tasks, minus everything that's now opencode's job
  (custom agent loop, tool-callback machinery, per-sub-agent prompt
  engineering for delegation).
- Some v1 backlog items (RAG store, scheduled briefings, proactive
  suggestions, multi-modal image input) are deliberately **not**
  included here — they're future-sprint material and should be
  re-evaluated once v2's core (items #1–17) is stable.
- Priorities are a starting point; adjust when filing if the opencode
  spike (#1) reveals that some items are trivial (opencode handles it
  natively) or much harder than expected.

## References

- Prior project's task history (multi-agent memory design, Phase 1–3
  rollout, deployment spike/design, NixOS/HM modules, CI task) — use as
  a checklist for "did we cover everything".
- opencode docs/repo (to be reviewed as part of task #1).

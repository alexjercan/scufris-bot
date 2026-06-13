# Plan scufris-v2: opencode-based rewrite backlog

- STATUS: CLOSED
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

- [x] Backlog above reviewed against v1 scope for gaps (anything from
      scufris-bot's feature set that's missing here is added as a new
      row before filing).
- [x] One `tatr` task filed per backlog row above, with the given
      title/priority/tags and a short (1–3 sentence) description.
- [x] Sequencing notes captured (at minimum: #1 and #2 block most
      other tasks; tools (#4–8) should land before or alongside the
      clients that exercise them; #21–24 can proceed in parallel with
      the server/client work once #1/#2 settle the package layout).
- [x] Each filed task cross-references this planning doc (e.g.
      "Spun off from scufris-v2 planning doc").

## Filed tasks

The 32 spun-off tasks (27 from the backlog above + 2 v1-scope gap items
added during review + 1 spike-driven follow-up + 2 design-doc
follow-ups — see Notes), in filing order:

| # | Task ID            | P  | Title                                                                          |
|---|--------------------|----|--------------------------------------------------------------------------------|
| 1 | `20260613-091035`  | 95 | Spike: opencode as agent runtime — sessions, tools, plugin API, config model   |
| 2 | `20260613-091036`  | 90 | Design doc: scufris-v2 architecture around opencode                            |
| 3 | `20260613-091037`  | 70 | Spike: per-(user, agent) session/context model on top of opencode              |
| 4 | `20260613-091038`  | 85 | Port journal tools (today/daily/macros) as opencode tools                      |
| 5 | `20260613-091039`  | 75 | Port weather/web_search/calculator/datetime tools to opencode                  |
| 6 | `20260613-091040`  | 40 | OS agent tools (file management, system monitor) for opencode                  |
| 7 | `20260613-091041`  | 30 | Calendar (CalDAV/ICS) tools for opencode                                       |
| 8 | `20260613-091042`  | 30 | Reminder / NL date parsing tools for opencode                                  |
| 9 | `20260613-091043`  | 90 | scufris-server v2: HTTP daemon wrapping opencode serve                         |
| 10| `20260613-091044`  | 85 | Per-user opencode session management (create/resume/expire)                    |
| 11| `20260613-091045`  | 75 | SSE streaming of opencode 'thinking' events                                    |
| 12| `20260613-091046`  | 75 | Identity layer + XDG user config (config.toml)                                 |
| 13| `20260613-091047`  | 50 | /stats and /clear endpoints + per-user telemetry                               |
| 14| `20260613-091048`  | 40 | Auth: bearer token for non-localhost binds                                     |
| 15| `20260613-091049`  | 80 | scufris-cli v2: REPL client over HTTP                                          |
| 16| `20260613-091050`  | 75 | Telegram bot v2: client over HTTP                                              |
| 17| `20260613-091051`  | 20 | CLI UX polish: autocomplete, multiline history, status pane                    |
| 18| `20260613-091052`  | 60 | Persistent conversation/session store (SQLite)                                 |
| 19| `20260613-091053`  | 40 | User facts store + remember/forget tools                                       |
| 20| `20260613-091054`  | 35 | History compaction strategy for opencode context windows                       |
| 21| `20260613-091056`  | 70 | Nix flake: package scufris-server-v2 and scufris-cli-v2 (uv2nix, flake-parts)  |
| 22| `20260613-091057`  | 60 | NixOS module + hardened systemd unit                                           |
| 23| `20260613-091058`  | 55 | Home Manager module                                                            |
| 24| `20260613-091059`  | 50 | CI: GitHub Actions for ruff/pytest/mypy/nix flake check                        |
| 25| `20260613-091100`  | 30 | Secrets/config injection guide (env-file / sops-nix / agenix)                  |
| 26| `20260613-091101`  | 45 | Test harness with mocked opencode client                                       |
| 27| `20260613-091102`  | 15 | Decommission/retire old LangChain-based scufris-bot                            |
| 28| `20260613-091103`  | 50 | Server observability: request IDs, structured JSON logs, /metrics endpoint    *(gap-fill)* |
| 29| `20260613-091104`  | 35 | Spike: scufris-server-v2 performance baseline (latency, memory, throughput)   *(gap-fill)* |
| 30| `20260613-093108`  | 73 | Permissions UX bridge: opencode permission.asked → Telegram inline buttons    *(spike #1 follow-up)* |
| 31| `20260613-093911`  | 70 | Plugin scaffolding: .opencode/plugin/scufris.ts loader, build, test path      *(design #2 follow-up)* |
| 32| `20260613-093857`  | 60 | Plugin ↔ scufris-server protocol: facts HTTP contract + auth token model      *(design #2 follow-up)* |

## Sequencing

A sketch of which tasks block which. Priorities are aligned to
roughly match this order, but the dependencies below override raw
priority where they conflict.

- **Foundations first.**  #1 (opencode capabilities spike) and #2
  (architecture design doc) block essentially every other task. #1
  feeds #2; #2 feeds the rest. Don't pick up implementation work until
  both have landed.
- **Session-model spike scopes the memory tasks.**  #3 (per-(user,
  agent) session/context spike) is what tells us whether we still need
  #18 (SQLite persistent store), #19 (user facts store), and #20
  (compaction). All three should wait on #3's output.
- **Tools before clients.**  #4–#8 (journal / web / OS / calendar /
  reminder tools) should land before or alongside #15 (CLI v2) and
  #16 (Telegram v2) so the clients have something interesting to
  exercise. The tool ports themselves can run in parallel with each
  other once #1/#2 settle the tool-registration shape.
- **Server before clients.**  #9 (server v2) is a hard prerequisite
  for #15 and #16. #10 (session management) and #11 (SSE thinking
  events) are part of the server-v2 surface; ideally they land in the
  same wave.
- **Identity layer is cross-cutting.**  #12 (identity + XDG config)
  affects #9, #10, #13, #15, #16. Easier to land #12 with #9 than to
  retrofit it.
- **Observability and stats can ride alongside the server.**  #11,
  #13, and #28 (gap-fill server observability) all touch the same
  request/streaming path; sequence them together once #9 has a stable
  shape.
- **Packaging / ops can run in parallel with server/client work.**
  #21 (Nix flake) only needs the v2 package layout to be settled
  (output of #2). Once #21 lands, #22 (NixOS module), #23 (Home
  Manager module), and #24 (CI) can proceed in parallel with each
  other and with the server/client tracks. #25 (secrets doc) is a
  late-stage docs task.
- **Quality gates land last in each wave.**  #26 (mocked opencode
  test harness) is most useful once #9 and a couple of tools exist
  to exercise it, but starting it earlier as a stub is fine. #29
  (gap-fill perf baseline spike) should wait until #9 is functional
  enough to bench against a stubbed opencode endpoint.
- **#27 (decommission v1) is terminal.**  Don't touch until v2 is in
  production use and parity has been verified.

## Notes

- This is intentionally a near 1:1 re-scoping of the v1 project's
  completed + open tasks, minus everything that's now opencode's job
  (custom agent loop, tool-callback machinery, per-sub-agent prompt
  engineering for delegation).
- Some v1 backlog items (RAG store, scheduled briefings, proactive
  suggestions, multi-modal image input) are deliberately **not**
  included here — they're future-sprint material and should be
  re-evaluated once v2's core (items #1–17) is stable.
- During the v1-scope review, two further v1 backlog items were
  identified that did not appear in the original 27-row backlog and
  were not on the explicit deferral list above: server observability
  (v1 `20260513-121622`) and a server perf baseline spike (v1
  `20260513-121621`). They were filed as gap-fill items #28 and #29
  with priorities (50, 35) chosen to match their v1-equivalent role
  (operational, not blocking the core).
- A further follow-up surfaced during the opencode runtime spike
  (#1, `20260613-091035`): a permissions UX bridge to relay opencode's
  `permission.asked` events into Telegram inline-keyboard prompts and
  post replies back to `POST /session/:id/permissions/:permID`. Filed
  as #30 (`20260613-093108`) at priority 73 — slightly below the
  Telegram bot v2 client (#16) which it depends on, and a hard
  prerequisite before any destructive tool (`bash`, `edit`, `write`,
  `apply_patch`, custom shell-spawning tools) is enabled in
  production.
- Two further follow-ups surfaced while writing the v2 architecture
  design doc (#2, `20260613-091036`):
  - #31 (`20260613-093911`, priority 70) — plugin scaffolding & build
    path. Decides where `.opencode/plugin/scufris.ts` lives, how
    opencode loads it, whether it needs a bundler step, and how it's
    tested. Logically precedes #19 (facts store) since the facts
    tools live in this plugin.
  - #32 (`20260613-093857`, priority 60) — plugin ↔ scufris-server
    HTTP protocol for facts and compaction-time context injection,
    plus the `SCUFRIS_PLUGIN_TOKEN` auth model. Depends on #19 and
    #31; gated `/v1/internal/*` endpoints separate from the
    user-facing `/v1/*` surface.
- Other v1 sub-agent items (knowledge-agent improvements, coding-agent
  sandbox, sub-agent catalog spike) are deliberately **not** carried
  over: they're explicitly the kind of "custom agent loop / per-sub-
  agent prompt engineering" work that opencode now owns.
- Priorities are a starting point; adjust when filing if the opencode
  spike (#1) reveals that some items are trivial (opencode handles it
  natively) or much harder than expected.

## References

- Prior project's task history (multi-agent memory design, Phase 1–3
  rollout, deployment spike/design, NixOS/HM modules, CI task) — use as
  a checklist for "did we cover everything".
- opencode docs/repo (to be reviewed as part of task #1).

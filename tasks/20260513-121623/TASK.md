# Spike: catalog candidate sub-agents and capabilities for v2

- STATUS: OPEN
- PRIORITY: 0
- TAGS: spike,design,agents

## Goal

Survey the open design space for "what new sub-agents should scufris
have?" and produce a short ranked list with concrete recommendations,
so subsequent agent-implementation tasks have a clear target.

## Scope

### In
- Audit current sub-agents (`coding`, `knowledge`, `utilities`,
  `journal`) and document their actual usage patterns from telemetry
  (which one gets called most, which is rarely useful, which has tool
  bloat).
- Brainstorm 6–10 candidate new agents. Examples to consider:
  - `home_assistant_agent` (zigbee2mqtt / Home Assistant API)
  - `media_agent` (mpd / mpv / spotify control)
  - `email_agent` (imap read-only summariser)
  - `shopping_list_agent` (reuses journal storage)
  - `research_agent` (web_search + summarise + cite)
  - `system_monitor_agent` (proc / disk / battery)
  - `file_management_agent` (search, organise — see refactor task)
  - `notes_agent` (markdown vault: dendron / obsidian)
- For each candidate, fill out a 1-paragraph rubric:
  - User stories it unlocks (3 examples)
  - Tools it would need (which exist? which need building?)
  - Memory profile (does it need history? long-term store?)
  - Estimated implementation cost (S/M/L)
  - Privacy / data-egress concerns
- Produce `tasks/<spike-id>/DESIGN.md` with the ranked list and a
  recommended top-3 to actually build in subsequent sprints.

### Out
- Implementing any agent — produce follow-up tasks instead.
- Tool implementation for the candidates.

## Acceptance criteria

- DESIGN.md exists with the rubric filled in for ≥6 candidates.
- A clear ranked top-3 with rationale.
- Each top-3 entry has a corresponding `tatr new` follow-up task
  filed (priority assigned by the ranking).
- The current-agent audit numbers come from real telemetry data
  (`utils/telemetry.py` log dump), not guesses.

## Notes

- The existing utilities-agent split is its own task — don't double
  up; just reference it.
- Keep the rubric tight; the goal is "decide what's worth doing", not
  "design it fully".

## References

- `tasks/20260509-154912/TASK.md` — earlier multi-agent design discussion.
- `utils/agent_builder.py` — current registration patterns.
- `tasks/20260513-121625/TASK.md` — utilities → os agent refactor.

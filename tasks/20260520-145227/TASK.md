# Morning briefing and scheduled agent prompts via timetable

- STATUS: OPEN
- PRIORITY: 0
- TAGS: journal,automation,ux,backlog

Two pieces: a timetable the agent knows about, and a dispatcher that fires prompts at the right times.

## Timetable Definition

A structured config (TOML or a dedicated journal section) defining named activity slots: `wake_up`, `breakfast`, `work`, `lunch`, `gym`, `hobby`, `dinner`, `sleep`.

Each slot has: time (or cron expression for flexible days), days of week, and a briefing type.

Example:

```toml
[[slots]]
name = "morning"
time = "07:30"
days = ["mon","tue","wed","thu","fri"]
briefing = "morning"

[[slots]]
name = "gym"
time = "18:00"
days = ["mon","wed","fri"]
briefing = "gym"
```

## Dispatcher

- `scufris-server` runs a background asyncio task (like the reminder dispatcher) that checks the timetable and fires agent prompts at slot times
- Each briefing type maps to a system prompt injected into a fresh turn: the agent is told "it's time for X, generate a briefing"
- Delivery via the active surface: Telegram message, or a CLI desktop notification (`notify-send`) if the CLI is not open

## Briefing Types

- **morning** — today's tasks from journal, habits to complete, weather, any due reminders, one-line motivational note
- **gym** — today's workout if defined in journal, last session logged, current weight trend
- **lunch** — remaining macros for the day, quick suggestion for what to eat to stay on target
- **evening** — incomplete tasks to roll over, habits not yet ticked, summary prompt ("ready to log dinner?")
- **sleep** — brief EOD summary, tomorrow's first task, reminder to log weight

## Cron Integration

- `scufris-cron` script (or `systemd.timers` entry in the NixOS/HM module) that calls `POST /v1/cron/slot {user_id: 42, slot: "morning"}` on the server
- Alternatively: server reads the timetable and self-schedules using asyncio — no external cron needed, but cron is more reliable across suspends/wakeups
- For multi-user installs, the cron entry iterates over registered users; for single-user it's a single call

## Dependencies on User Identity (`20260520-145231`)

This task is a **hard dependency** on the identity layer. The whole point of scheduled briefings is they deliver to a specific human across whatever surface they're currently using:

- **Timetable location**: lives in the per-user XDG config as `[[user.schedule.slots]]` (already drafted in the identity task). The dispatcher reads each registered user's config on startup and on `SIGHUP`.
- **Timezone**: pulled from `user.timezone` — the dispatcher computes "is it 07:30 in *this user's* zone?" not server-local time. Critical if the box is in UTC.
- **Surface routing**: each slot has a `surfaces = ["telegram"]` list. The dispatcher resolves the user's bindings (`surface_bindings` table) to a concrete delivery target — Telegram chat ID, CLI session token, etc.
- **Active surface fallback**: if a slot specifies `surfaces = ["active"]`, the dispatcher delivers to whichever surface had the most recent inbound activity (queried from `messages.ts`).
- **User-scoped agent invocation**: the briefing prompt runs through the normal agent stack with that user's `user_id` so it reads the correct journal, facts, RAG sources, and history.

**Implementation order**: identity layer **must** land before this task (decided). A degraded v0 (single hardcoded user, server-local time) is possible but throws away most of the value.

### Missed-slot handling (decided)

When a slot fires but no surface is reachable (laptop closed, phone off), the dispatcher **queues** the briefing and delivers it when the user next becomes active, prefixed with `(missed your morning briefing — here it is)`. Bounded by a max-age cutoff per briefing type so a missed morning isn't delivered at 6 PM:

```python
MAX_DELIVERY_AGE = {
  "morning": timedelta(hours=4),
  "lunch":   timedelta(hours=3),
  "gym":     timedelta(hours=2),
  "evening": timedelta(hours=3),
  "sleep":   timedelta(hours=2),
}
```

Past the cutoff, the queued briefing is silently dropped. Requires a small `pending_briefings(user_id, slot, scheduled_at, content)` table.

## Briefing Generation Pipeline

To make briefings useful, each briefing type is essentially a parameterized agent prompt + tool whitelist:

```python
BRIEFINGS = {
  "morning": {
    "agent": "journal",
    "prompt": "Generate a morning briefing for {user}. Include: today's tasks, "
              "uncompleted habits, weather for {city}, due reminders. Keep under 200 words.",
    "tools": ["tasks_list", "habits_list", "weather", "reminders_due"],
  },
  ...
}
```

The dispatcher hydrates `{user}`, `{city}`, etc. from the user's facts + config, then runs a single agent turn with the result delivered to the configured surfaces.

## Complexity Estimate

Medium. The async scheduler + slot evaluation is small (~200 lines). Surface dispatch is small once identity is wired. The real work is **content quality** — making each briefing actually useful rather than rote, which is a prompt iteration loop.

## Open Questions

- **Snooze / dismiss**: should the user be able to reply "skip today" or "remind me in 30 min"? Adds state.
- **Briefing as conversation starter**: does the briefing message create a normal history entry the user can reply to, or is it a one-shot notification?
- **Quiet hours**: a per-user "do not disturb 22:00–07:00" override that suppresses non-essential slots? Or just rely on slot definitions being correct?
- **Cross-surface dedup**: if both Telegram and CLI are active, do you want the briefing on both or on one (most recently active)? My instinct: most-recently-active by default, with an explicit `surfaces = ["all"]` opt-in.
- **External vs internal scheduler**: do you actually want `systemd.timers` (survives crashes, NixOS-native) or is the in-process asyncio scheduler fine given the server is supervised already?

## Decided

- **Sequencing**: identity layer (`20260520-145231`) is a hard prerequisite
- **Missed slots**: queue and deliver when surface returns, bounded by per-type max-age cutoffs

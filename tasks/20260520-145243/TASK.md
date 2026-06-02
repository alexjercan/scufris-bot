# Proactive follow-up suggestions in agent prompts

- STATUS: OPEN
- PRIORITY: 60
- TAGS: ux,prompts,backlog

Primarily a prompt engineering task, but with a small structural hook.

## Mechanism

- Each sub-agent prompt gets a `## Proactive Suggestions` section listing situations where it should append a short follow-up offer at the end of its response
- The main agent prompt instructs Scufris to surface these naturally, not robotically

## Examples Per Agent

### Journal agent
- After logging macros → "You're 340 cal under target. Want me to suggest something to fill the gap?"
- After toggling a habit → "3 of 5 habits done today. Want to see which are still open?"
- After adding a task → "You have 6 tasks for today. Want me to prioritize them?"

### Knowledge agent
- After a weather lookup → "Should I add a 'bring umbrella' reminder for tomorrow morning?"
- After a factual answer → "Want me to save this to your notes?"

### Coding agent
- After explaining a bug → "Want me to open this file in OpenCode and apply the fix?"

## Implementation

- No new code needed initially — just prompt additions
- Later: a structured `suggestion` field in the agent's response JSON that the CLI/Telegram renders as a tappable/clickable affordance (e.g., `[y] Yes, suggest meals` prompt after a macro log)

## Dependencies on User Identity (`20260520-145231`) and Facts (`20260520-145244`)

**Light** dependency, but becomes powerful once both land:

- **Dismissal memory**: if the user says "no thanks" to a suggestion three times, the facts layer remembers `dont_suggest_meal_filler = true` and the agent stops offering. Without persistent facts, the agent re-asks every session — annoying.
- **Per-user suggestion style**: a config knob like `[user.ux] proactive_level = "low" | "medium" | "high"` controls how often the agent volunteers suggestions. Some users want a nudge engine, others find it noise.
- **Cross-surface coherence**: a suggestion offered in CLI ("want me to remind you?") shouldn't be re-offered when the user switches to Telegram mid-conversation — requires shared history (the identity layer gives this).

Can ship as prose-only prompt additions immediately with zero deps; the smart version waits for facts + identity.

## Suggestion Taxonomy

Worth being explicit about *categories* so prompts can be tuned per type and users can opt out of specific kinds:

- **Action follow-up**: "I did X, want me to also do Y?" (low risk, usually welcome)
- **Reminder offer**: "should I remind you about this?" (medium — needs reminder system to actually exist)
- **Insight surfacing**: "you've logged this 5 days in a row" (high value, but easy to overdo)
- **Cross-agent handoff**: "this looks like a coding question, want me to ask the coding agent?" (useful, but should be silent dispatch in most cases)

## Complexity Estimate

Tiny for v1 (a few hours of prompt edits). Medium if you want the structured `suggestion` JSON field with CLI/Telegram rendering, dismissal tracking, and per-user opt-out — that's a week.

## Open Questions

- **Where do suggestions appear visually?** Inline in the response, on a separate line with a marker (`💡 ...` or `→ ...`), or only when the structured field is present?
- **Telegram inline keyboards**: tap-to-accept suggestions are very nice on mobile but require the structured field + bot button plumbing. Worth it?
- **Suggestion rate-limit**: cap at 1 suggestion per response? Per N minutes? Otherwise the agent becomes a needy assistant.
- **Negative reinforcement loop**: how does dismissal actually get captured — explicit "no" detection in NLU, or a `/dismiss` slash command, or just count "no" replies in the next turn?

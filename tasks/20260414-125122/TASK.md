# Improve daily journal agent

- STATUS: IN_PROGRESS
- PRIORITY: 100
- TAGS: feature

---

## Current State Analysis

### Existing Capabilities
- ✅ 5 tools: today_create, daily_view, macros_lookup, macros_entry, notes_entry
- ✅ Journal sections: Habits, Tasks, Macros, Weight, Notes
- ✅ Strong food tracking workflow with mandatory lookup
- ✅ Clean code with good error handling

### Identified Gaps
- ❌ No habit tracking/completion tools
- ❌ No task management tools
- ❌ No weight logging tools
- ❌ No search/query tools for fuzzy food lookup
- ❌ No food database insertion capability
- ❌ No analytics/trends features
- ❌ Limited historical viewing

---

## Phase 1: Quick Wins (No CLI Changes Required)

### Agent Tools to Add
- [x] `macros_search_tool` - Fuzzy search for foods (uses existing `macros -q`)
- [x] `macros_insert_tool` - Add new foods to database (uses existing `macros -i`)
- [x] `notes_filter_tool` - View notes by tag (uses existing `daily --note`)

### Prompt Improvements
- [x] Add guidance for food search workflow
- [x] Add instructions for handling food not found
- [x] Document journal structure in prompt
- [x] Add encouraging language for habit tracking
- [x] Add proactive suggestion guidelines

### Code Updates
- [x] Add new tools to `utils/tools/journal_tools.py`
- [x] Export new tools in `utils/tools/__init__.py`
- [x] Update `JOURNAL_AGENT_PROMPT` in `utils/agent_builder.py`
- [x] Update `create_journal_agent()` to include new tools

---

## Phase 2: Habit Tracking

### CLI Commands to Implement (in `daily`)
- [ ] `--toggle-habit <HABIT>` - Toggle habit checkbox completion

### Agent Tools to Add
- [ ] `habits_toggle_tool` - Toggle habit completion status

---

## Phase 3: Task Management

### CLI Commands to Implement (in `daily`)
- [ ] `--task-entry <TEXT>` - Add task to Today's Tasks section (the Today tasks must have a `[ ]` checkbox at the beginning of the line)
- [ ] `--task-tomorrow-entry <TEXT>` - Add task to Tomorrow's Tasks section (the Tomorrow tasks must not have a checkbox at the beginning of the line, just a `-` bullet point)
- [ ] `--toggle-task <INDEX>` - Mark task complete/incomplete by index

### Agent Tools to Add
- [ ] `tasks_entry_tool` - Add tasks to daily journal
- [ ] `tasks_tomorrow_entry_tool` - Add tasks to tomorrow's section in daily journal
- [ ] `tasks_toggle_tool` - Toggle task completion

---

## Phase 4: Weight Tracking

### CLI Commands to Implement (in `daily`)
- [ ] `--weight-entry <VALUE><UNIT>` - Log weight for the day, if we find a weight entry for the day we update it with the new value the weight is in format `weight :: VALUE <UNIT>` where VALUE is a number and UNIT is `Kg`.

### Agent Tools to Add
- [ ] `weight_entry_tool` - Add weight entry to daily journal

---

## Implementation Priority Summary

1. **Start Here** - Phase 1 (Quick Wins): Immediate value, no CLI changes
2. **High Value** - Phase 2 (Habits) + Phase 3 (Tasks): Core missing features
3. **Important** - Phase 4 (Weight): Complete tracking capabilities
5. **Future** - Additional CLI enhancements: Power user features

---

## Files to Modify

- `utils/tools/journal_tools.py` - Add new tool implementations
- `utils/tools/__init__.py` - Export new tools
- `utils/agent_builder.py` - Update prompt and agent configuration
- CLI tools (`daily`, `macros`, `today`) - Add new subcommands (separate repos)
    - These scripts can be found in `~/personal/nix.dotfiles/home/modules/scripts/daily.nix`
    - These scripts can be found in `~/personal/nix.dotfiles/home/modules/scripts/today.nix`
    - These scripts can be found in `~/personal/macros.nvim/macros.lua`

---

## Notes

- Phase 1 can be implemented immediately (uses existing CLI features)
- Phases 2-4 require implementing new CLI subcommands first
- Agent prompt improvements should emphasize:
  - Mandatory lookup workflow for food tracking
  - Helpful suggestions when food not found
  - Encouraging language for habit building
  - Proactive assistance (e.g., "Would you like to see your summary?")
- All new tools should follow existing error handling patterns
- Maintain consistency with current tool naming and documentation style

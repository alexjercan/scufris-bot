# Improve daily journal agent

- STATUS: CLOSED
- PRIORITY: 100
- TAGS: feature

---

## ⭐ Implementation Summary

### ✅ All Phases Complete (Phases 1-4)
The journal agent now has **15 tools** covering all core functionality:
- Journal entry management (2 tools)
- Food tracking (4 tools)
- Notes management (2 tools)
- Habit tracking (1 tool)
- Task management (5 tools)
- Weight tracking (1 tool)

### ✅ Historical Viewing Already Available
**Discovery**: The `daily_view_tool` already supports historical viewing via the `offset` parameter!
- No new tool needed - functionality already exists
- Updated prompt to clarify offset usage for the agent
- Examples: offset=-1 (yesterday), offset=-7 (last week)

### ✅ Note Tag Pattern Added
Added guidance to prompt for the `note :: TAG` pattern:
- When user says "add a note about X", agent now knows to format as:
  ```
  note :: X

  <actual note content>
  ```
- This enables better organization and filtering with `notes_filter_tool`

### 🚫 Future Enhancements - Not Implementing (Yet)
Based on user feedback, keeping it simple. Not implementing:
- Analytics tools (streaks, averages, trends)
- Bulk operations
- Smart features (task migration, meal suggestions)
- Meal timing tracking (user does this manually as needed)

These may be revisited in the future but are not priorities now.

---

## Current State Analysis

### Existing Capabilities
- ✅ **15 tools total** - Comprehensive journal management
- ✅ **Journal Entry Management (2)**: today_create, daily_view
- ✅ **Food Tracking (4)**: macros_lookup, macros_search, macros_entry, macros_insert
- ✅ **Notes (2)**: notes_entry, notes_filter
- ✅ **Habit Tracking (1)**: habits_toggle
- ✅ **Task Management (5)**: tasks_entry, tasks_tomorrow_entry, tasks_toggle, tasks_remove, tasks_tomorrow_remove
- ✅ **Weight Tracking (1)**: weight_entry
- ✅ Strong food tracking workflow with mandatory lookup
- ✅ Clean code with good error handling

### Completed Phases
- ✅ Phase 1: Quick wins (macros_search, macros_insert, notes_filter, prompt improvements)
- ✅ Phase 2: Habit tracking (habits_toggle)
- ✅ Phase 3: Task management (5 task tools)
- ✅ Phase 4: Weight tracking (weight_entry)

### Potential Future Enhancements (Phase 5+)
See sections below for detailed proposals on:
- Historical viewing (offset-based lookups)
- Analytics (streaks, trends, completion rates)
- Convenience tools (summaries, bulk operations)
- Smart features (task migration, meal suggestions)

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
- [x] `--toggle-habit <HABIT>` - Toggle habit checkbox completion

### Agent Tools to Add
- [x] `habits_toggle_tool` - Toggle habit completion status

---

## Phase 3: Task Management

### CLI Commands to Implement (in `daily`)
- [x] `--task-entry <TEXT>` - Add task to Today's Tasks section (the Today tasks must have a `[ ]` checkbox at the beginning of the line)
- [x] `--task-tomorrow-entry <TEXT>` - Add task to Tomorrow's Tasks section (the Tomorrow tasks must not have a checkbox at the beginning of the line, just a `-` bullet point)
- [x] `--toggle-task <INDEX>` - Mark task complete/incomplete by index
- [x] `--task-remove <INDEX>` - Remove task from Today's Tasks by index
- [x] `--task-tomorrow-remove <INDEX>` - Remove task from Tomorrow by index

### Agent Tools to Add
- [x] `tasks_entry_tool` - Add tasks to daily journal
- [x] `tasks_tomorrow_entry_tool` - Add tasks to tomorrow's section in daily journal
- [x] `tasks_toggle_tool` - Toggle task completion
- [x] `tasks_remove_tool` - Remove tasks from Today's Tasks
- [x] `tasks_tomorrow_remove_tool` - Remove tasks from Tomorrow

---

## Phase 4: Weight Tracking

### CLI Commands to Implement (in `daily`)
- [x] `--weight-entry <VALUE><UNIT>` - Log weight for the day, if we find a weight entry for the day we update it with the new value the weight is in format `weight :: VALUE <UNIT>` where VALUE is a number and UNIT is `Kg`.

### Agent Tools to Add
- [x] `weight_entry_tool` - Add weight entry to daily journal

---

## Phase 5: Additional Prompt Enhancements

### Implemented
- [x] **Historical viewing support** - Clarified that `daily_view_tool` offset parameter enables viewing past entries
- [x] **Note tag pattern** - Added guidance for "note :: TAG" formatting when user requests tagged notes

### Future Considerations (Not Implementing Yet)
These features were explored but decided against for now to keep the system simple:

#### Historical Viewing Tools (Not Needed)
- ~~`journal_view_offset_tool`~~ - Already available via `daily_view_tool` offset parameter
- ~~`journal_view_range_tool`~~ - Not needed yet

#### Analytics Tools (Future - Not Implementing)
These would require CLI development and are not current priorities:
- ~~`habits_streak_tool`~~ - Show current streak for a habit
- ~~`habits_stats_tool`~~ - Show completion stats for habits
- ~~`macros_weekly_average_tool`~~ - Show average macros over last 7 days
- ~~`weight_trend_tool`~~ - Show weight trend over time period
- ~~`tasks_completion_rate_tool`~~ - Show % of tasks completed recently

#### Convenience Tools (Future - Not Implementing)
- ~~`journal_summary_tool`~~ - Generate daily summary
- ~~`macros_daily_total_tool`~~ - Just show total macros for today
- ~~`tasks_list_incomplete_tool`~~ - Show only incomplete tasks from today

#### Bulk Operations (Future - Not Implementing)
- ~~`macros_bulk_entry_tool`~~ - Add multiple food entries at once
- ~~`tasks_bulk_entry_tool`~~ - Add multiple tasks at once

#### Smart Suggestions (Future - Not Implementing)
- ~~`journal_copy_yesterday_tool`~~ - Copy incomplete tasks (Tomorrow→Today already automated)
- ~~`macros_suggest_meal_tool`~~ - Suggest meals to hit macro targets

---

## Phase 6: Observations from Usage Patterns

### Patterns Observed from Journal Entries
1. **Timestamps in macros** - User sometimes adds timestamps (9:00 AM, 12:00 PM) to track meal timing
   - **Decision**: Not automating - user does this manually as needed
2. **Tomorrow → Today workflow** - Tasks in Tomorrow section move to Today's Tasks
   - **Decision**: Already automated by `today --create` command
3. **Tags in notes** - User uses formatting like `TODO(timestamp):`, `note ::`, `weight ::`
   - **Decision**: Added prompt guidance for `note :: TAG` pattern
4. **Habit variations** - Habits change over time (e.g., "📱 < 1 Hour of brainrot")
   - **Decision**: Not implementing dynamic habit management yet

### Additional Enhancement Ideas (Not Implementing)
These were considered but keeping it simple for now:
- ~~Time tracking for meals~~ - Manual timestamps sufficient
- ~~Automated task migration~~ - Already handled by journal creation
- ~~Dynamic habit management~~ - Not a priority
- ~~Advanced tag organization~~ - Current system works well
- ~~Meal timing analysis~~ - Not needed yet

---

## Implementation Priority Summary

1. ✅ **COMPLETED** - Phase 1 (Quick Wins): macros_search, macros_insert, notes_filter, prompt improvements
2. ✅ **COMPLETED** - Phase 2 (Habits): habits_toggle tool
3. ✅ **COMPLETED** - Phase 3 (Tasks): 5 task management tools
4. ✅ **COMPLETED** - Phase 4 (Weight): weight_entry tool
5. ✅ **COMPLETED** - Phase 5 (Enhancements): Historical viewing clarification, note tag pattern
6. 🚫 **NOT IMPLEMENTING** - Analytics, bulk operations, smart features (keeping it simple)

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

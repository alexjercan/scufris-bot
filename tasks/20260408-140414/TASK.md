# Implement daily journal agent

- STATUS: IN_PROGRESS
- PRIORITY: 100
- TAGS: feature

The Daily Journal Agent will manage `the-den` journal. It will wrap useful
commands like `daily`, `today` and `macros`.

TODOs:
- [x] We will need to write wrapper tools for each CLI command
- [x] `today --create` will create the today's journal entry if it doesn't exist yet
- [x] `daily --macros-entry "SOME TEXT"` this sub-command adds the text (which can be multi line) in the `### 🍽️ Macros` section of the current daily
- [x] `daily --notes-entry "SOME TEXT"` this sub-command adds the text (which can be multi line) in the `### 📝 Notes` section of the current daily
- [x] `macros "chicken breast 100g"` this sub-command computes `chicken breast 100g,31,0,4` the macros for the given food item, the food item should be something like `<name> <qty><unit>`
- [x] The macros command should be used to compute the macros for whatever food item the user provides and then we use the `--macros-entry` tool to actually persist the food item and macros
- [x] `daily` outputs to stdout a compact view of the Today's journal entry, with details about food, tasks and stuff

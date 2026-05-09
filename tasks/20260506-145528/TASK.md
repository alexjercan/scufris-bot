# Create a CLI tool for the bot that we can use instead of telegram

- STATUS: OPEN
- PRIORITY: 100
- TAGS: feature,cli

We need to implement a simple CLI python script (sort of a REPL chat) that
let's us have the same basic interface we have in telegram. I want to be able
to write and edit messages and send them to the bot. Then get back the response
in the CLI (maybe with escape codes for colors). Not necessarily a TUI
application, but a simple debug purposes CLI one that let's you do REPL. Maybe
it can use rlwrap or something like that to make it easy to write messages.

TLDR: We need a REPL style CLI tool that let's us chat with the bot


# Create a simple coding agent that uses Opencode

- STATUS: OPEN
- PRIORITY: 100
- TAGS: feature

Instead of doing the full blown orchestrator agent I want to simplify things
and keep just a coding agent. The structure should be "Coding Agent" ->
Opencode as a tool. Then the coding agent should keep in it's history chat
buffer the stuff from opencode. And it should also be able to tell the user (in
the CLI or telegram message) if they need to input something. For example when
Opencode needs access from the user to "write" or "read" a file and we need to
press "yes" we should show that to the user.

Currently I have an open question: How can we make this async? Opencode runs
async, you can "queue" new messages and it just keeps running. But if we call
it from an external source how will we be able to do that nicely?


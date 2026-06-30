# Address SSE-payload `type` asymmetry

- STATUS: OPEN
- PRIORITY: 50
- TAGS: server,client,sdk

The SSE payload `type` field in the server's response has an asymmetry with the client/SDK expectations (e.g., terminology mismatch in tool events).

Unify the `type` taxonomy across the server and the client/SDK to ensure consistent event handling.

- [ ] Audit all SSE event types in `scufris_server`
- [ ] Audit all event handling logic in `scufris_client` and `scufris_cli`
- [ ] Implement changes to align types
- [ ] Verify with integration tests

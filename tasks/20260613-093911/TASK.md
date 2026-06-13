# Plugin scaffolding: .opencode/plugin/scufris.ts loader, build, test path

- STATUS: OPEN
- PRIORITY: 70
- TAGS: opencode,plugin,infra

Establish how `.opencode/plugin/scufris.ts` is structured, loaded by
opencode at startup, and tested. Lock down four decisions before any
plugin logic is written:

1. **Layout.** One `.opencode/plugin/scufris.ts` checked into the repo,
   per design-doc ADR-9. No npm publishing, no separate package.
2. **Build.** Decide whether opencode loads raw TypeScript directly or
   needs a bundled JS artifact (esbuild / tsc). Determine if any
   third-party imports require bundling.
3. **Loader.** Verify how opencode discovers plugins (filename
   convention vs. `opencode.json` declaration); document the exact
   path opencode follows on startup.
4. **Test path.** Decide between (a) in-process tests using opencode's
   plugin types directly, (b) integration tests via a real
   `opencode serve` instance with a fixture project, or (c) both. The
   first is fast but couples to opencode internals; the second is
   slower but verifies actual hook behaviour.

Acceptance:
- `.opencode/plugin/scufris.ts` skeleton committed (no real logic
  yet — just the default export with empty hook stubs).
- README or `docs/plugin.md` notes how to rebuild/reload the plugin
  during development.
- A test command (e.g. `pytest tests/plugin/`) runs at least one test
  exercising the plugin's tool registration.

Surfaced as a follow-up while writing the v2 architecture design doc
(`tasks/20260613-091036/TASK.md` — see §5.3, §10.2, §17). Logically
precedes #19 (facts store + tools) and #32 (plugin protocol contract);
should land alongside or just before #19.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).

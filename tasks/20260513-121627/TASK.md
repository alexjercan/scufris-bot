# Coding agent: multi-language support and sandboxed code execution

- STATUS: OPEN
- PRIORITY: 0
- TAGS: agents,coding

## Goal

The `coding_agent` currently delegates everything to `opencode_tool`,
which is great for editing files in place but has no way to actually
*run* code to verify a fix or compute a quick answer. Add a
sandboxed execution tool that supports a small set of languages, and
make the prompt aware of when "run it" is the right move vs. "edit
and hand back".

## Scope

### In
- Add `code_exec_tool` with a language-tagged interface:
  `code_exec(language, source, stdin="", timeout_s=10)`.
- Supported languages for v1: `python`, `bash`, `node`, `lua`.
  Resolved via Nix-pinned interpreters so reproducibility is
  guaranteed in deployed builds.
- Sandbox via `bubblewrap` (Linux) or `sandbox-exec` (macOS):
  - no network,
  - read-only `/nix/store`,
  - writable `tmpfs` for `/tmp` and `$HOME`,
  - CPU + memory + wall-clock limits.
- Update `CODING_AGENT_PROMPT` to teach the model:
  - run small scripts to *verify* edits before reporting done,
  - never run code that touches the user's real files,
  - cap iteration count (≤3 exec calls per turn) to avoid loops.
- Output schema: `{stdout, stderr, exit_code, duration_ms, truncated}`.
- Tests: smoke test per language, sandbox-escape regression test
  (attempt network call, attempt to write outside tmpdir).

### Out
- Compiled languages (Rust/Go/C++) — punt to v2 once we have a
  caching story for build artefacts.
- Persistent REPL state across exec calls.
- GPU / CUDA workloads.

## Acceptance criteria
- `code_exec(language="python", source="print(2+2)")` returns
  `stdout="4\n"`, `exit_code=0`.
- Sandbox-escape tests fail closed (network call returns non-zero,
  write to `$HOME/real-file` fails).
- A coding-agent eval prompt like "write a function that reverses
  a string and verify it works on three inputs" results in at least
  one `code_exec` call and a verified answer.
- Nix flake exposes the four interpreters via a single
  `codeExecEnv` derivation.

## Notes
- Reuse the resource-limit settings from
  `tasks/20260423-101533/TASK.md` (opencode-tool cgroup work) — same
  philosophy.
- Bubblewrap config lives in `nix/sandbox.nix` (new file).

## References
- `utils/agent_builder.py:557` — current `create_coding_agent`.
- `utils/tools/opencode_tool.py` — existing tool reference pattern.
- `tasks/20260513-121623/TASK.md` — agent v2 spike.

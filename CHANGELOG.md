# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-14

Initial release of the scufris bot.

### Added

#### Agents
- Daily journal agent for personal note-keeping, with subsequent improvements
  to its prompts and behavior.
- Multi-agent memory and prompt synergy: per-agent prompt rework (Phase 1),
  context argument plumbed through sub-agent delegations (Phase 2), and
  per-(user, agent) sub-agent history (Phase 3).
  - Rekeyed `ChatHistoryManager` by `(user_id, agent)`.
  - Plumbed `user_id` via `RunnableConfig.configurable`.
  - Wired sub-agent history load + persist in `create_sub_agent`.
- History compaction system:
  - Phase 1: `Compactor` protocol with summary/facts storage and eviction wiring.
  - Phase 2: `LLMCompactor` with summary/facts injection into prompt assembly.
  - Phase 3: `remember`/`forget` tools, thinking-trace integration, and `/stats` polish.

#### CLI
- New CLI tool as an alternative interface to the Telegram bot.
- Surfaced agent thinking as chat messages in the CLI.
- Polished thinking UI: display names, sub-agent context, and styling.
- CLI thinking trace renders a `+N prior turns` hint.
- Prettier `/stats` table formatting.

#### Server & Deployment
- HTTP server daemon entrypoint (`scufris-server`).
- CLI reworked as an HTTP client of `scufris-server`.
- Nix flake packaging for both `scufris-server` and `scufris-cli`.
- NixOS module with hardened systemd unit for `scufris-server`.
- Home Manager module for user-level installs.
- Systemd daemon for the bot, packaged via the Nix flake.
- GitHub Actions CI: ruff, pytest, mypy, and `nix flake check`.

#### Telegram
- Richer `/stats` command with per-agent breakdown.
- `/clear` now wipes all per-agent slices.

#### Tooling & Infrastructure
- Improved structured logging across the project.
- Telemetry experiments folder for analyzing sub-agent context stats.
- Unit tests added for: `utils/telemetry.py`, `utils/callbacks.py`,
  `utils/stats.py`, pure tools (calculator, datetime), HTTP tools (weather,
  web_search, opencode) with mocks, and `journal_tools` with subprocess mocks.

### Changed
- Replaced `datetime.utcnow()` with timezone-aware `datetime.now(UTC)` in
  `history.py`.
- Resolved pre-existing mypy errors.

### Removed
- Deprecated `/history` Telegram/CLI command.

### Fixed
- `weather_tool`: converted to `@tool` decorator and added forecast horizon
  support.

[0.1.0]: https://github.com/anomalyco/scufris-bot/releases/tag/v0.1.0

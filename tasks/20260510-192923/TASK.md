# Secrets and config injection: sops-nix vs EnvironmentFile

- STATUS: OPEN
- PRIORITY: 0
- TAGS: deploy,security

## Goal

Decide and document how secrets (Telegram bot token, scufris bearer
token, future API keys) reach the deployed `scufris-server` in a way
that's safe to commit to a public repo and works on both single-user
laptops and shared/multi-user hosts.

## Scope

### In
- Short comparison doc (`docs/secrets.md` or README section) of:
  - **Plain `EnvironmentFile=`** — file outside the Nix store, root-readable,
    written by hand or external tooling. Simplest. Recommended baseline.
  - **sops-nix** — encrypted secrets in the repo, decrypted at activation
    using age/GPG keys. Good for multi-host fleets and GitOps.
  - **agenix** — similar to sops-nix, age-only, smaller surface.
  - **systemd-creds** — built-in, host-bound encryption; not portable
    across hosts but zero extra deps.
- Pick a default for the NixOS module: keep `environmentFile` as the
  primary contract (already in task 20260510-192748), provide a
  documented sops-nix recipe as an opt-in pattern.
- Identify which env vars are actual secrets vs config:
  - **Secrets:** `SCUFRIS_TOKEN` (server bearer), `TELEGRAM_BOT_TOKEN`,
    any future cloud LLM API keys.
  - **Config (not secret):** `SCUFRIS_MODEL`, `SCUFRIS_OLLAMA_URL`,
    `SCUFRIS_BIND`, `SCUFRIS_PORT`. Live in module options, not the
    secret file.
- Document the env-file format with an example:
  ```
  SCUFRIS_TOKEN=...
  TELEGRAM_BOT_TOKEN=...
  ```
- Verify file permissions guidance: `0400 root:root`, or
  `0400 :scufris` if a static group is used.
- Update the NixOS and Home Manager module docs to point at this guide.

### Out
- Implementing actual sops-nix integration in the flake (consumers add
  it themselves; we just provide a recipe).
- Secret rotation tooling.
- HSM / vault integrations.

## Acceptance criteria

- A clear "how do I deploy this safely?" section exists in the docs.
- The recommended path (env-file) is shown end-to-end with file perms,
  systemd hookup, and a sample file.
- The sops-nix opt-in path is shown as a copy-pasteable example.
- No secret values, example or otherwise, are committed in formats that
  could be mistaken for real ones (use obvious placeholders like
  `xxxx-REPLACE-ME`).
- The module's `environmentFile` option docstring links to this guide.

## Notes

- Single-user laptop case: a `chmod 600` file in `~/.config/scufris/env`
  + Home Manager `server.environmentFile` is plenty.
- Multi-tenant / shared host case: sops-nix or agenix is worth the
  setup cost; document the tradeoffs but don't force the choice.
- Keep this task small — it's mostly a docs deliverable plus a config
  audit, not new code.

## References

- `tasks/20260510-192748/TASK.md` — NixOS module's `environmentFile`.
- `tasks/20260510-192825/TASK.md` — Home Manager equivalent.
- sops-nix: https://github.com/Mic92/sops-nix
- agenix: https://github.com/ryantm/agenix
- systemd-creds: https://systemd.io/CREDENTIALS/

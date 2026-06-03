# Secrets and config injection

Where do `SCUFRIS_TOKEN` and `TELEGRAM_BOT_TOKEN` live, and how do they
reach the running `scufris-server` without ending up in the Nix store
or in a public Git repo?

## TL;DR

- **Plain `EnvironmentFile=`** — write the file by hand, point the
  module at it. **This is the recommended baseline** and is what both
  flake modules expose as their primary contract.
- **sops-nix / agenix** — encrypt the same file in the repo, decrypt
  at activation. Worth the setup cost for multi-host fleets and
  GitOps, but optional.
- **systemd-creds** — host-bound, zero extra deps, not portable.
  Mentioned for completeness; not currently wired up.

The module options never take a secret as a Nix value — `settings`
goes into `/etc/scufris/config.toml` (world-readable in `/etc`) so
anything in there is **public on the host**.

## What's a secret?

| Variable                 | Secret? | Where                                                   |
| ------------------------ | ------- | ------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`     | yes     | env-file → `[telegram].bot_token` override              |
| `SCUFRIS_TOKEN`          | yes     | env-file → `[server].token` override                    |
| Future cloud LLM API keys| yes     | env-file                                                |
| `OLLAMA_MODEL`, `OLLAMA_BASE_URL` | no | `settings.ollama.*`                                |
| `SCUFRIS_BIND`, `SCUFRIS_PORT`    | no | `settings.server.{bind,port}`                      |
| `ALLOWED_USER_IDS`       | no      | `settings.telegram.allowed_user_ids`                    |

Env vars override matching TOML keys at load time — that's the entire
mechanism. Secrets stay env-only because the env layer is the only one
the module never touches at evaluation time.

## File format

`KEY=value` per line, `#` comments allowed, no shell expansion (this
is `EnvironmentFile=`, not `bash`). Example with obvious placeholders:

```ini
# /etc/scufris/env  (NixOS)  or  ~/.config/scufris/env  (Home Manager)
TELEGRAM_BOT_TOKEN=xxxx-REPLACE-ME-bot-token
SCUFRIS_TOKEN=xxxx-REPLACE-ME-bearer-token
```

Permissions — **the deployment target dictates the owner**, not your
muscle memory. Mixing these up will produce systemd's least-helpful
error message (`Result: resources` / `Failed to load environment
files: Permission denied`):

| Deployment                             | Mode  | Owner             |
| -------------------------------------- | ----- | ----------------- |
| Home Manager (per-user, systemd `--user`) | `0600`| `$USER:$USER`     |
| NixOS (`DynamicUser` — the default)    | `0400`| `root:root`       |
| NixOS (static `services.scufris.user`) | `0400`| `:scufris` (or `0440 root:scufris`) |

For Home Manager this means: **do not** `sudo install` the file — the
user systemd instance runs as you, not root, and won't be able to
read a root-owned file even at `0444`. Use `install` (or just `cat >`)
as your own user.

systemd reads the file before dropping privileges on system units, so
root-owned is fine on NixOS even with `DynamicUser`.

## Recipe 1 — plain env-file (recommended)

### NixOS

```nix
{
  services.scufris = {
    enable = true;
    settings = {
      ollama.model = "qwen3:latest";
      server.port = 8765;
    };
    environmentFile = "/etc/scufris/env";   # outside the Nix store
  };
}
```

Then create the file out of band (e.g. via your provisioning tool, or
by hand on a single host):

```bash
sudo install -m 0400 -o root -g root /dev/stdin /etc/scufris/env <<'EOF'
TELEGRAM_BOT_TOKEN=xxxx-REPLACE-ME
SCUFRIS_TOKEN=xxxx-REPLACE-ME
EOF
```

Both flake modules pass the path through as `EnvironmentFile=` with a
leading `-`, so a missing file does **not** crashloop the unit — it
just starts without those overrides. Useful on first boot.

### Home Manager

```nix
{
  programs.scufris = {
    enable = true;
    settings = { /* ... */ };
    environmentFile = "${config.home.homeDirectory}/.config/scufris/env";
    server.enable = true;
    bot.enable    = true;
  };
}
```

Create the file as **your own user** (not via `sudo install`, which
would leave it root-owned and unreadable to the user systemd
instance):

```bash
install -m 0600 -D /dev/stdin ~/.config/scufris/env <<'EOF'
TELEGRAM_BOT_TOKEN=xxxx-REPLACE-ME
SCUFRIS_TOKEN=xxxx-REPLACE-ME
EOF
```

Don't manage this file via `xdg.configFile."scufris/env".text` — that
puts the contents in the Nix store, which is world-readable. A
placeholder via `home.activation` is fine if you want HM to scaffold
an empty file on first switch; real secrets stay outside the store.

The CLI does **not** auto-source this file. If you need
`SCUFRIS_TOKEN` for `scufris-cli` in your shell, source it yourself:

```bash
# in ~/.bashrc / ~/.zshrc / etc.
if [ -r "$HOME/.config/scufris/env" ]; then
  set -a
  . "$HOME/.config/scufris/env"
  set +a
fi
```

## Recipe 2 — sops-nix (opt-in)

[sops-nix](https://github.com/Mic92/sops-nix) decrypts age- or
GPG-encrypted secrets at activation time and writes them to a
configurable path. Pair it with the module's `environmentFile` option:

### NixOS

```nix
{
  imports = [
    inputs.sops-nix.nixosModules.sops
    inputs.scufris.nixosModules.default
  ];

  sops = {
    defaultSopsFile = ./secrets/scufris.yaml;
    age.keyFile = "/var/lib/sops-nix/key.txt";

    secrets."scufris/env" = {
      # Renders to /run/secrets/scufris/env at activation. The systemd
      # unit reads this path; no plaintext on disk.
      mode  = "0400";
      owner = "root";
      group = "root";
    };
  };

  services.scufris = {
    enable = true;
    settings = { /* ... */ };
    environmentFile = config.sops.secrets."scufris/env".path;
  };
}
```

`secrets/scufris.yaml` (committed, encrypted):

```yaml
scufris:
  env: |
    TELEGRAM_BOT_TOKEN=xxxx-REPLACE-ME
    SCUFRIS_TOKEN=xxxx-REPLACE-ME
```

Edit with `sops secrets/scufris.yaml`. The `|` block scalar matters —
it preserves newlines so systemd parses each `KEY=value` correctly.

### Home Manager

[sops-nix's HM module](https://github.com/Mic92/sops-nix#home-manager)
exposes the same shape per-user:

```nix
{
  imports = [
    inputs.sops-nix.homeManagerModules.sops
    inputs.scufris.homeManagerModules.default
  ];

  sops = {
    defaultSopsFile = ./secrets/scufris.yaml;
    age.keyFile = "${config.home.homeDirectory}/.config/sops/age/keys.txt";
    secrets."scufris/env" = { mode = "0400"; };
  };

  programs.scufris = {
    enable = true;
    settings = { /* ... */ };
    environmentFile = config.sops.secrets."scufris/env".path;
    server.enable = true;
  };
}
```

## Recipe 3 — agenix (opt-in)

[agenix](https://github.com/ryantm/agenix) is the same idea as sops-nix
but age-only and a smaller surface. The wiring is identical from the
scufris module's perspective:

```nix
{
  age.secrets.scufris-env = {
    file  = ./secrets/scufris.age;
    mode  = "0400";
    owner = "root";
  };

  services.scufris = {
    enable = true;
    settings = { /* ... */ };
    environmentFile = config.age.secrets.scufris-env.path;
  };
}
```

Pick agenix if you only need age and want fewer moving parts; pick
sops-nix if you want age **or** PGP, multi-recipient YAML/JSON
secrets, or you already use sops elsewhere.

## Recipe 4 — systemd-creds (mentioned, not wired)

[systemd-creds](https://systemd.io/CREDENTIALS/) encrypts secrets to
the host's TPM/system key. It's built in, requires no extra Nix
inputs, but the ciphertext is host-bound — you can't share an encrypted
blob across hosts the way sops-nix lets you.

To use it today, write the cipher with `systemd-creds encrypt` and
load it via `LoadCredentialEncrypted=` on the unit. The scufris
module does not currently expose a credentials option; if you want
this you need a small NixOS overlay on `systemd.services.scufris`:

```nix
{
  systemd.services.scufris.serviceConfig.LoadCredentialEncrypted = [
    "scufris.env:/etc/scufris/scufris.env.cred"
  ];
  # The unit then reads $CREDENTIALS_DIRECTORY/scufris.env, but
  # scufris-server expects EnvironmentFile= — wrap it with a tiny
  # ExecStartPre that copies the cred into a tmpfs path you point
  # `services.scufris.environmentFile` at, or contribute upstream.
}
```

This is enough friction that env-file or sops-nix are the better
defaults for now.

## Comparison

| Mechanism            | Setup cost | Repo-safe? | Multi-host? | Extra deps     |
| -------------------- | ---------- | ---------- | ----------- | -------------- |
| Plain env-file       | tiny       | no (file is *.gitignore'd*) | manual copy | none           |
| sops-nix             | medium     | yes (encrypted) | yes         | sops-nix flake |
| agenix               | medium     | yes (encrypted) | yes         | agenix flake   |
| systemd-creds        | medium     | yes (encrypted) | no (host-bound) | none       |

Recommendation: start with the plain env-file. Move to sops-nix or
agenix the first time you have more than one host, or the moment you
want to put your secrets in version control.

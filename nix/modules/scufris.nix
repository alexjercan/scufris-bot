# NixOS module: system-wide scufris-server install.
#
# Configuration mirrors the TOML schema 1:1: `services.scufris.settings`
# is a free-form attrset rendered to `/etc/scufris/config.toml` via
# `pkgs.formats.toml`. The systemd unit gets `SCUFRIS_CONFIG` pointing
# at that file and otherwise inherits no Scufris-specific environment.
#
# Secrets (telegram bot token, server token) are still injected via
# `environmentFile` — env vars override matching TOML keys at load
# time, so secrets stay out of the Nix store while everything else
# lives declaratively in the module.
{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.scufris;

  tomlFormat = pkgs.formats.toml {};
  configFile = tomlFormat.generate "scufris-config.toml" cfg.settings;

  # Pull the listen port out of `settings` so `openFirewall` and the
  # documentation refer to the same value the daemon will actually use.
  # Falls back to the application default when unset.
  effectivePort = cfg.settings.server.port or 8765;
in {
  options.services.scufris = {
    enable = lib.mkEnableOption "Scufris HTTP agent server";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.scufris-server;
      defaultText = lib.literalExpression "pkgs.scufris-server";
      description = "The scufris-server package to run.";
    };

    settings = lib.mkOption {
      type = tomlFormat.type;
      default = {};
      example = lib.literalExpression ''
        {
          user.username = "alex";
          user.timezone = "Europe/Berlin";
          user.identity = {
            telegram = 8231376426;
            cli = "alex";
          };
          ollama = {
            model = "qwen3:14b";
            base_url = "http://127.0.0.1:11434";
          };
          server = {
            bind = "127.0.0.1";
            port = 8765;
          };
          telegram.allowed_user_ids = [ 8231376426 ];
        }
      '';
      description = ''
        Free-form attrset rendered verbatim into
        `/etc/scufris/config.toml`. The full schema is documented in
        `utils/config.py`. Secrets (`telegram.bot_token`,
        `server.token`) belong in `environmentFile`, not here — env
        vars override the TOML at load time so the secret never enters
        the Nix store.
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/run/secrets/scufris.env";
      description = ''
        Path to a file containing `KEY=value` lines with secrets:
        `TELEGRAM_BOT_TOKEN`, `SCUFRIS_TOKEN`. Loaded by systemd at
        unit start so secrets never end up in the Nix store. Env vars
        override matching TOML keys (`telegram.bot_token`,
        `server.token`) at load time.

        See `tasks/20260510-192923/DESIGN.md` for deployment patterns
        (plain env-file, sops-nix, agenix, systemd-creds) and
        file-permission guidance.
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the configured port in the system firewall.";
    };

    user = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Static user to run the service as. When null (default) the unit
        uses systemd DynamicUser, which is the recommended setup.
      '';
    };

    group = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Static group; pairs with `user`. Null means DynamicUser.";
    };

    memoryDenyWriteExecute = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Enable MemoryDenyWriteExecute. Off by default because some
        Python ML libraries (ctypes, torch, JIT-using code) need W+X
        pages and will crash. Turn on after verifying with the real
        model backend.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    networking.firewall.allowedTCPPorts =
      lib.mkIf cfg.openFirewall [effectivePort];

    environment.etc."scufris/config.toml".source = configFile;

    systemd.services.scufris = {
      description = "Scufris HTTP agent server";
      wantedBy = ["multi-user.target"];
      after = ["network-online.target"];
      wants = ["network-online.target"];

      # The only env var the unit needs: where to find its config.
      # All other settings live in the TOML; secrets come from
      # `environmentFile`.
      environment.SCUFRIS_CONFIG = "/etc/scufris/config.toml";

      serviceConfig =
        {
          # The server does not implement sd_notify; "simple" is correct.
          Type = "simple";
          ExecStart = "${cfg.package}/bin/scufris-server";
          Restart = "on-failure";
          RestartSec = 5;

          # Server's app.py honours [server].shutdown_grace (default 30s)
          # and waits for in-flight requests on SIGTERM. Give it a few
          # extra seconds before systemd escalates to SIGKILL.
          TimeoutStopSec = 35;
          KillSignal = "SIGTERM";

          StateDirectory = "scufris";
          StateDirectoryMode = "0750";
          RuntimeDirectory = "scufris";

          # ----- hardening -----
          ProtectSystem = "strict";
          ProtectHome = true;
          PrivateTmp = true;
          PrivateDevices = true;
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectKernelLogs = true;
          ProtectControlGroups = true;
          ProtectClock = true;
          ProtectHostname = true;
          ProtectProc = "invisible";
          ProcSubset = "pid";
          NoNewPrivileges = true;
          RestrictNamespaces = true;
          RestrictRealtime = true;
          RestrictSUIDSGID = true;
          LockPersonality = true;
          RestrictAddressFamilies = ["AF_INET" "AF_INET6" "AF_UNIX"];
          SystemCallFilter = ["@system-service" "~@privileged" "~@resources"];
          SystemCallArchitectures = "native";
          CapabilityBoundingSet = "";
          AmbientCapabilities = "";
          UMask = "0077";
          PrivateUsers = true;
        }
        // lib.optionalAttrs (cfg.environmentFile != null) {
          EnvironmentFile = cfg.environmentFile;
        }
        // lib.optionalAttrs cfg.memoryDenyWriteExecute {
          MemoryDenyWriteExecute = true;
        }
        // (
          if cfg.user == null
          then {DynamicUser = true;}
          else {
            User = cfg.user;
            Group = lib.mkIf (cfg.group != null) cfg.group;
          }
        );
    };
  };
}

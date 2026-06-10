# NixOS module: supervise `opencode serve` as a system service.
#
# Hard prereq for the OpenCode-runtime swap (`tasks/20260610-101413`):
# `scufris-server` talks HTTP to a local OpenCode daemon for every
# chat turn. This module gives that daemon a unit, a static port, a
# state directory for its session store, and an environment-file slot
# for provider credentials (GITHUB_TOKEN, etc.).
#
# Pairs with `nix/modules/scufris.nix`: when both are enabled, the
# scufris unit auto-discovers `OPENCODE_BASE_URL` and orders itself
# `After=opencode-serve.service` with `Wants=` (soft dep — `/v1/readyz`
# returns degraded if OpenCode is down, but the public HTTP endpoint
# stays up).
{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.opencode-serve;
in {
  options.services.opencode-serve = {
    enable = lib.mkEnableOption "OpenCode HTTP daemon (`opencode serve`)";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.opencode;
      defaultText = lib.literalExpression "pkgs.opencode";
      description = ''
        The OpenCode package providing `bin/opencode`. The Scufris
        runtime was developed against the 1.15.x wire protocol
        (server-global `/event` SSE bus, `message.part.delta`
        text events). Older releases — including the version pinned
        by this repo's `nixpkgs` input at the time of writing
        (1.3.10) — emit incompatible event shapes; override this
        option with a newer build before deploying.
      '';
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        Listen address. Default loopback — OpenCode is a privileged
        runtime (it can run shell commands via tools when not gated)
        and must not be exposed to untrusted clients. Set to
        `0.0.0.0` only behind a TLS-terminating reverse proxy with
        bearer auth in front.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 4096;
      description = ''
        Listen port. Default matches OpenCode's `serve` default and
        the value the spike used. The Scufris service reads
        `OPENCODE_BASE_URL=http://${"\${host}"}:${"\${port}"}` from
        its environment; no port literal lives in Python.
      '';
    };

    url = lib.mkOption {
      type = lib.types.str;
      default = "http://${cfg.host}:${toString cfg.port}";
      defaultText = lib.literalExpression ''"http://''${cfg.host}:''${toString cfg.port}"'';
      readOnly = true;
      description = ''
        Computed base URL. Other modules (notably `services.scufris`)
        read this to wire `OPENCODE_BASE_URL` so the port lives in
        exactly one place.
      '';
    };

    workingDirectory = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/etc/scufris/skills";
      description = ''
        Working directory for the daemon. OpenCode resolves
        `AGENTS.md` and the `skills/` tree relative to this path.
        When null (default) the unit uses its `StateDirectory`
        (`/var/lib/opencode-serve`); drop an `AGENTS.md` there or
        point this at a checkout with the skill set.
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/run/secrets/opencode.env";
      description = ''
        Path to a file with `KEY=value` lines providing provider
        credentials (`GITHUB_TOKEN`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
        …). Loaded by systemd at unit start so secrets never enter
        the Nix store. Without this the daemon still starts and
        serves `/session` and `/event`, but chat turns fail at the
        provider boundary.
      '';
    };

    logLevel = lib.mkOption {
      type = lib.types.enum ["DEBUG" "INFO" "WARN" "ERROR"];
      default = "INFO";
      description = "OpenCode `--log-level` flag value.";
    };

    user = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Static user to run as. When null (default) the unit uses
        systemd `DynamicUser`, which is the recommended setup; the
        daemon's auth/session state lives under `StateDirectory` and
        survives restarts but not user-id reassignments.
      '';
    };

    group = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Static group; pairs with `user`. Null means DynamicUser.";
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      example = ["--print-logs"];
      description = ''
        Extra arguments appended to `opencode serve`. Default unit
        already passes `--port`, `--hostname`, `--log-level` and
        `--print-logs` (so output reaches journald). Use this for
        flags that aren't yet first-class options here.
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Open `port` in the system firewall. Off by default because
        the recommended deployment binds to loopback.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    networking.firewall.allowedTCPPorts =
      lib.mkIf cfg.openFirewall [cfg.port];

    systemd.services.opencode-serve = {
      description = "OpenCode HTTP daemon";
      wantedBy = ["multi-user.target"];
      # Provider calls go to the public internet; wait for the network
      # before starting so the first chat turn doesn't hit DNS races.
      after = ["network-online.target"];
      wants = ["network-online.target"];

      # Force the daemon to write its `auth.json` and session state
      # under StateDirectory rather than $HOME (which is /var/empty
      # under DynamicUser). OpenCode honours XDG paths; we point them
      # at a writeable location.
      environment = {
        HOME = "/var/lib/opencode-serve";
        XDG_DATA_HOME = "/var/lib/opencode-serve/share";
        XDG_CONFIG_HOME = "/var/lib/opencode-serve/config";
        XDG_CACHE_HOME = "/var/lib/opencode-serve/cache";
        XDG_STATE_HOME = "/var/lib/opencode-serve/state";
      };

      serviceConfig =
        {
          Type = "simple";
          ExecStart = lib.escapeShellArgs (
            [
              "${cfg.package}/bin/opencode"
              "serve"
              "--port"
              (toString cfg.port)
              "--hostname"
              cfg.host
              "--log-level"
              cfg.logLevel
              "--print-logs"
            ]
            ++ cfg.extraArgs
          );

          Restart = "on-failure";
          RestartSec = 5;
          # Provider calls can run for tens of seconds; give the daemon
          # a sane stop timeout before SIGKILL.
          TimeoutStopSec = 30;
          KillSignal = "SIGTERM";

          StateDirectory = "opencode-serve";
          StateDirectoryMode = "0750";
          RuntimeDirectory = "opencode-serve";

          # ----- hardening -----
          # The daemon needs: outbound HTTPS to provider APIs, read
          # access to the working directory's AGENTS.md / skills,
          # write access to its state dir. No filesystem mutations
          # outside those.
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
          # AF_NETLINK is needed by some Bun internals (DNS via
          # systemd-resolved) on certain hosts; keep AF_INET/INET6
          # for outbound provider calls.
          RestrictAddressFamilies = ["AF_INET" "AF_INET6" "AF_UNIX" "AF_NETLINK"];
          SystemCallFilter = ["@system-service" "~@privileged" "~@resources"];
          SystemCallArchitectures = "native";
          CapabilityBoundingSet = "";
          AmbientCapabilities = "";
          UMask = "0077";
          PrivateUsers = true;
        }
        // lib.optionalAttrs (cfg.workingDirectory != null) {
          WorkingDirectory = cfg.workingDirectory;
        }
        // lib.optionalAttrs (cfg.workingDirectory == null) {
          WorkingDirectory = "/var/lib/opencode-serve";
        }
        // lib.optionalAttrs (cfg.environmentFile != null) {
          EnvironmentFile = cfg.environmentFile;
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

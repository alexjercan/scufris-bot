{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.scufris;

  # Build the Environment= list. Always include bind/port. Include
  # ollama bits only when the user set them so we don't override
  # whatever the application defaults to.
  baseEnv =
    {
      SCUFRIS_BIND = cfg.bind;
      SCUFRIS_PORT = toString cfg.port;
      SCUFRIS_LOG_LEVEL = cfg.logLevel;
    }
    // lib.optionalAttrs (cfg.model != null) {OLLAMA_MODEL = cfg.model;}
    // lib.optionalAttrs (cfg.ollamaUrl != null) {OLLAMA_BASE_URL = cfg.ollamaUrl;}
    // cfg.extraEnvironment;
in {
  options.services.scufris = {
    enable = lib.mkEnableOption "Scufris HTTP agent server";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.scufris-server;
      defaultText = lib.literalExpression "pkgs.scufris-server";
      description = "The scufris-server package to run.";
    };

    bind = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Address the HTTP server binds to (SCUFRIS_BIND).";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8765;
      description = "TCP port the HTTP server listens on (SCUFRIS_PORT).";
    };

    logLevel = lib.mkOption {
      type = lib.types.enum ["DEBUG" "INFO" "WARNING" "ERROR" "CRITICAL"];
      default = "INFO";
      description = "Log level forwarded as SCUFRIS_LOG_LEVEL.";
    };

    model = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "qwen3:latest";
      description = ''
        Ollama model name. Mapped to OLLAMA_MODEL — the env var the
        agent reads (the task spec calls this SCUFRIS_MODEL; the option
        keeps the friendlier name).
      '';
    };

    ollamaUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "http://127.0.0.1:11434";
      description = "Ollama base URL. Mapped to OLLAMA_BASE_URL.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/run/secrets/scufris.env";
      description = ''
        Path to a file containing KEY=value lines with secrets
        (SCUFRIS_TOKEN, TELEGRAM_BOT_TOKEN, ...). Loaded by systemd at
        unit start time so secrets never end up in the Nix store.
      '';
    };

    extraEnvironment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = {};
      example = {SCUFRIS_SHUTDOWN_GRACE = "20";};
      description = "Additional Environment= entries for the unit.";
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
    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [cfg.port];

    systemd.services.scufris = {
      description = "Scufris HTTP agent server";
      wantedBy = ["multi-user.target"];
      after = ["network-online.target"];
      wants = ["network-online.target"];

      environment = baseEnv;

      serviceConfig =
        {
          # The server does not implement sd_notify; "simple" is correct.
          Type = "simple";
          ExecStart = "${cfg.package}/bin/scufris-server";
          Restart = "on-failure";
          RestartSec = 5;

          # Server's app.py honours SCUFRIS_SHUTDOWN_GRACE (default 30s)
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

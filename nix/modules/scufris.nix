{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.scufris;
  inherit (lib) mkOption types;
in {
  options.services.scufris = {
    enable = lib.mkEnableOption "Whether to enable the scufris-server daemon.";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.scufris-server;
      defaultText = lib.literalExpression "pkgs.scufris-server";
      description = "The scufris-server package to run.";
    };

    opencodeModel = mkOption {
      type = types.nullable types.str;
      default = null;
      description = ''
        Override the default model probed from the opencode daemon.
        Set to `providerID/modelID` (e.g. `"ollama/qwen3:latest"`)
        or just a model ID to use `ollama` as the provider.
        When `null`, the server probes `GET /provider` on startup.
      '';
    };

    bind = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = "Interface scufris-server binds to.";
    };

    port = mkOption {
      type = types.port;
      default = 7080;
      description = "TCP port scufris-server listens on.";
    };

    opencodeUrl = mkOption {
      type = types.str;
      default = "http://127.0.0.1:4096";
      description = "Base URL of the opencode serve daemon.";
    };

    opencodePassword = mkOption {
      type = types.nullable types.str;
      default = null;
      description = "Bearer token expected by opencode.";
    };

    stateDir = mkOption {
      type = types.str;
      default = "";
      description = ''
        Directory for SQLite DB and other persistent state.
        Empty string uses the default (`$XDG_STATE_HOME/scufris`).
      '';
    };

    config = mkOption {
      type = types.lines;
      default = "";
      description = ''
        User configuration (`config.toml`) content.
        See `examples/config.toml` for the expected format.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.scufris-server = {
      description = "Scufris HTTP agent server";
      wantedBy = ["multi-user.target"];
      wants = ["network-online.target"];
      after = ["network-online.target"];

      environment =
        {
          SCUFRIS_BIND = cfg.bind;
          SCUFRIS_PORT = toString cfg.port;
          OPENCODE_URL = cfg.opencodeUrl;
        }
        // lib.optionalAttrs (cfg.opencodePassword != null) {
          OPENCODE_SERVER_PASSWORD = cfg.opencodePassword;
        }
        // lib.optionalAttrs (cfg.opencodeModel != null) {
          OPENCODE_MODEL = cfg.opencodeModel;
        }
        // lib.optionalAttrs (cfg.stateDir != "") {
          SCUFRIS_STATE_DIR = cfg.stateDir;
        };

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

    xdg.configFile."scufris/config.toml" = lib.mkIf (cfg.config != "") {
      text = cfg.config;
    };
  };
}

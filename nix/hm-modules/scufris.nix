# Home Manager module: user-level scufris install.
#
# Mirrors the option layout of `nixosModules.scufris` so users can move
# between system-wide and per-user installs without relearning.
#
# Closes over the flake's `self` so the default `package` options
# resolve via `self.packages.${system}.<name>` rather than requiring
# the consumer to inject a `pkgs.scufris-*` overlay.
{self}: {
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.programs.scufris;
  flakePkgs = self.packages.${pkgs.system};

  serverEnv =
    {
      SCUFRIS_BIND = cfg.server.bind;
      SCUFRIS_PORT = toString cfg.server.port;
      SCUFRIS_LOG_LEVEL = cfg.server.logLevel;
    }
    // lib.optionalAttrs (cfg.server.model != null) {
      OLLAMA_MODEL = cfg.server.model;
    }
    // lib.optionalAttrs (cfg.server.ollamaUrl != null) {
      OLLAMA_BASE_URL = cfg.server.ollamaUrl;
    }
    // cfg.server.extraEnvironment;

  defaultClientUrl = "http://${cfg.server.bind}:${toString cfg.server.port}";

  clientSessionVars =
    lib.optionalAttrs cfg.server.enable {
      SCUFRIS_SERVER_URL = defaultClientUrl;
    }
    // cfg.clientEnvironment;
in {
  options.programs.scufris = {
    enable = lib.mkEnableOption "scufris-cli in the user profile";

    package = lib.mkOption {
      type = lib.types.package;
      default = flakePkgs.scufris-cli;
      defaultText = lib.literalExpression "scufris.packages.\${system}.scufris-cli";
      description = "The scufris-cli package to install.";
    };

    clientEnvironment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = {};
      example = {
        SCUFRIS_SERVER_URL = "http://127.0.0.1:8765";
        SCUFRIS_TOKEN = "supersecret";
      };
      description = ''
        Extra session variables for the client. Merged into
        `home.sessionVariables` and takes precedence over the auto-set
        `SCUFRIS_SERVER_URL` from `programs.scufris.server`.

        Note: `home.sessionVariables` only affects newly started shells
        — re-source your shell or log out/in for them to take effect.
      '';
    };

    server = {
      enable = lib.mkEnableOption "scufris-server as a systemd --user service";

      package = lib.mkOption {
        type = lib.types.package;
        default = flakePkgs.scufris-server;
        defaultText = lib.literalExpression "scufris.packages.\${system}.scufris-server";
        description = "The scufris-server package to run as a user service.";
      };

      bind = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1";
        description = "Address the user-level server binds to.";
      };

      port = lib.mkOption {
        type = lib.types.port;
        default = 8765;
        description = "TCP port for the user-level server.";
      };

      logLevel = lib.mkOption {
        type = lib.types.enum ["DEBUG" "INFO" "WARNING" "ERROR" "CRITICAL"];
        default = "INFO";
        description = "Log level for the user-level server.";
      };

      model = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "qwen3:latest";
        description = "Ollama model name (mapped to OLLAMA_MODEL).";
      };

      ollamaUrl = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "http://127.0.0.1:11434";
        description = "Ollama base URL (mapped to OLLAMA_BASE_URL).";
      };

      environmentFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        example = "\${config.home.homeDirectory}/.config/scufris/env";
        description = ''
          Path to a KEY=value file with secrets (SCUFRIS_TOKEN, ...).
          Loaded by systemd at unit start so secrets stay out of the
          Nix store.
        '';
      };

      extraEnvironment = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        default = {};
        example = {SCUFRIS_SHUTDOWN_GRACE = "20";};
        description = "Extra Environment= entries for the unit.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages =
      [cfg.package]
      ++ lib.optional cfg.server.enable cfg.server.package;

    home.sessionVariables = clientSessionVars;

    systemd.user.services.scufris = lib.mkIf cfg.server.enable {
      Unit = {
        Description = "Scufris HTTP agent server (user)";
        After = ["network-online.target"];
        Wants = ["network-online.target"];
      };

      Service =
        {
          Type = "simple";
          ExecStart = "${cfg.server.package}/bin/scufris-server";
          Restart = "on-failure";
          RestartSec = 5;
          # Matches the server's 30s SCUFRIS_SHUTDOWN_GRACE drain.
          TimeoutStopSec = 35;
          KillSignal = "SIGTERM";

          # User-unit-friendly hardening. System-only options
          # (DynamicUser, CapabilityBoundingSet, Protect{Kernel,Proc},
          # PrivateUsers, ...) are intentionally omitted — they require
          # root and silently no-op or fail under --user.
          PrivateTmp = true;
          ProtectSystem = "strict";
          NoNewPrivileges = true;
          LockPersonality = true;
          RestrictRealtime = true;
          RestrictSUIDSGID = true;
          UMask = "0077";

          # systemd accepts repeated Environment= lines; HM serialises
          # a list of "KEY=value" strings into exactly that.
          Environment =
            lib.mapAttrsToList (n: v: "${n}=${v}") serverEnv;
        }
        // lib.optionalAttrs (cfg.server.environmentFile != null) {
          EnvironmentFile = toString cfg.server.environmentFile;
        };

      Install.WantedBy = ["default.target"];
    };
  };
}

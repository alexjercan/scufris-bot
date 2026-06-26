{self}: {
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.programs.scufris;
  flakePkgs = self.packages.${pkgs.system};
  inherit (lib) mkOption types;

  configRelPath = "scufris/config.toml";
  configAbsPath = "${config.xdg.configHome}/${configRelPath}";
in {
  options.programs.scufris = {
    enable = lib.mkEnableOption "scufris-cli in the user profile";

    package = lib.mkOption {
      type = lib.types.package;
      default = flakePkgs.scufris-cli;
      defaultText = lib.literalExpression "scufris.packages.\${system}.scufris-cli";
      description = "The scufris-cli package to install.";
    };

    server = {
      enable = lib.mkEnableOption "scufris-server as a systemd --user service";

      package = lib.mkOption {
        type = lib.types.package;
        default = flakePkgs.scufris-server;
        defaultText = lib.literalExpression "scufris.packages.\${system}.scufris-server";
        description = "The scufris-server package to run as a user service.";
      };

      opencodeModel = mkOption {
        type = types.nullable types.str;
        default = null;
        description = ''
          Override the default model probed from the opencode daemon.
          Set to `providerID/modelID` (e.g. `"ollama/qwen3:latest"`)
          or just a model ID to use `ollama` as the provider.
          When ``null``, the server probes ``GET /provider`` on startup.
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
          User configuration (``config.toml``) content.
          See ``examples/config.toml`` for the expected format.
        '';
      };
    };

    bot = {
      enable = lib.mkEnableOption "scufris-bot (Telegram) as a systemd --user service";

      package = lib.mkOption {
        type = lib.types.package;
        default = flakePkgs.scufris-bot;
        defaultText = lib.literalExpression "scufris.packages.\${system}.scufris-bot";
        description = "The scufris-bot package to run as a user service.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages =
      [cfg.package]
      ++ lib.optional cfg.server.enable cfg.server.package
      ++ lib.optional cfg.bot.enable cfg.bot.package;

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
          # Matches the server's [server].shutdown_grace 30s drain.
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

          # The unit doesn't inherit `home.sessionVariables`, so set
          # `SCUFRIS_CONFIG` explicitly here too.
          Environment = ["SCUFRIS_CONFIG=${configAbsPath}"];
        }
        // lib.optionalAttrs (cfg.environmentFile != null) {
          # Leading `-` tells systemd to ignore the file if it doesn't
          # exist yet, so the unit doesn't crashloop the first time a
          # user enables the module before populating their secrets.
          EnvironmentFile = "-${toString cfg.environmentFile}";
        };

      Install.WantedBy = ["default.target"];
    };

    systemd.user.services.scufris-bot = lib.mkIf cfg.bot.enable {
      Unit = {
        Description = "Scufris Telegram bot (user)";
        # Bot fails fast if the server is unreachable, so order it
        # after the server unit when both are enabled. Plain `After`
        # is enough — Restart=on-failure handles the startup race
        # if the server is still warming up.
        After =
          ["network-online.target"]
          ++ lib.optional cfg.server.enable "scufris.service";
        Wants =
          ["network-online.target"]
          ++ lib.optional cfg.server.enable "scufris.service";
      };

      Service =
        {
          Type = "simple";
          ExecStart = "${cfg.bot.package}/bin/scufris-bot";
          Restart = "on-failure";
          RestartSec = 10;
          KillSignal = "SIGTERM";

          # Same user-unit hardening profile as the server. The bot
          # only needs outbound HTTP (to Telegram + to scufris-server).
          PrivateTmp = true;
          ProtectSystem = "strict";
          NoNewPrivileges = true;
          LockPersonality = true;
          RestrictRealtime = true;
          RestrictSUIDSGID = true;
          UMask = "0077";

          Environment = ["SCUFRIS_CONFIG=${configAbsPath}"];
        }
        // lib.optionalAttrs (cfg.environmentFile != null) {
          # Same `-` soft-fail policy as the server unit. If the file
          # is missing or unreadable (a common HM footgun: installing
          # it with `sudo install -m 0400 -o root` from the NixOS
          # recipe instead of `chmod 600` as your own user), the
          # spawn-layer "Result: resources" failure is unhelpful —
          # let Python start instead and surface a real
          # "TELEGRAM_BOT_TOKEN missing" error.
          EnvironmentFile = "-${toString cfg.environmentFile}";
        };

      Install.WantedBy = ["default.target"];
    };

    xdg.configFile.configRelPath = lib.mkIf (cfg.config != "") {
      text = cfg.config;
    };

    home.activation.scufris-server = lib.mkIf (cfg.opencodeModel != null) ''
      # OPENCODE_MODEL environment variable is set via Home Manager
      # by adding it to the user's shell profile.
      if [ -z "$OPENCODE_MODEL" ]; then
        echo "OPENCODE_MODEL=${cfg.opencodeModel}" >> "$out/etc/profile.d/scufris.sh"
      fi
    '';
  };
}

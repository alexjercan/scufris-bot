# Home Manager module: per-user scufris install.
#
# Mirrors `nixosModules.scufris`: a single free-form `settings` attrset
# is rendered to `${XDG_CONFIG_HOME}/scufris/config.toml` via
# `pkgs.formats.toml`, and `SCUFRIS_CONFIG` is exported as a session
# variable so both the CLI in interactive shells and the optional
# `systemd --user` server unit read the same file.
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

  tomlFormat = pkgs.formats.toml {};
  configFile = tomlFormat.generate "scufris-config.toml" cfg.settings;
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

    settings = lib.mkOption {
      type = tomlFormat.type;
      default = {};
      example = lib.literalExpression ''
        {
          user.username = "alex";
          user.timezone = "Europe/Berlin";
          user.identity.cli = "alex";
          ollama.model = "qwen3:14b";
          client.server_url = "http://127.0.0.1:8765";
          server.bind = "127.0.0.1";
          server.port = 8765;
        }
      '';
      description = ''
        Free-form attrset rendered verbatim into
        `${"\${XDG_CONFIG_HOME}"}/scufris/config.toml`. Used by both the
        CLI (when reading `[client]`/`[user]`) and the optional
        user-level server (when reading `[server]`/`[ollama]`/...).
        Schema is documented in `utils/config.py`.

        Secrets (`server.token`, `telegram.bot_token`) belong in
        `programs.scufris.server.environmentFile` or your shell's own
        secret-loading mechanism — env vars override matching TOML
        keys at load time.
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

      environmentFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        example = "\${config.home.homeDirectory}/.config/scufris/env";
        description = ''
          Path to a KEY=value file with secrets (`SCUFRIS_TOKEN`,
          `TELEGRAM_BOT_TOKEN`, ...). Loaded by systemd at unit start
          so secrets stay out of the Nix store. Env vars override
          matching TOML keys at load time.
        '';
      };
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages =
      [cfg.package]
      ++ lib.optional cfg.server.enable cfg.server.package;

    xdg.configFile.${configRelPath}.source = configFile;

    # Both the CLI in interactive shells and the user unit below read
    # the same env var. `home.sessionVariables` only affects newly
    # started shells — re-source or re-login to pick it up.
    home.sessionVariables.SCUFRIS_CONFIG = configAbsPath;

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
        // lib.optionalAttrs (cfg.server.environmentFile != null) {
          # Leading `-` tells systemd to ignore the file if it doesn't
          # exist yet, so the unit doesn't crashloop the first time a
          # user enables the module before populating their secrets.
          EnvironmentFile = "-${toString cfg.server.environmentFile}";
        };

      Install.WantedBy = ["default.target"];
    };
  };
}

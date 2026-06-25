{ config, lib, pkgs, package, ... }:
let
  cfg = config.programs.scufris;
  inherit (lib) mkOption types;
in {
  options.programs.scufris = {
    enable = mkOption {
      type = types.bool;
      default = false;
      description = "Whether to enable scufris.";
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

  config = lib.mkIf cfg.enable {
    home.packages = [ package ];

    xdg.configFile."scufris/config.toml" = lib.mkIf (cfg.config != "") {
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

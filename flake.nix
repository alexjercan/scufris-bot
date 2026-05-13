{
  description = "Python Flake using pyproject-nix and uv2nix";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    flake-parts.url = "github:hercules-ci/flake-parts";
  };

  outputs = inputs @ {
    self,
    flake-parts,
    nixpkgs,
    pyproject-nix,
    uv2nix,
    pyproject-build-systems,
    ...
  }: let
    # The top-level abstraction in uv2nix is the workspace, which needs to be loaded.
    workspace = uv2nix.lib.workspace.loadWorkspace {workspaceRoot = ./.;};
    # Takes uv.lock & creates an overlay for use with pyproject.nix builders.
    overlay = workspace.mkPyprojectOverlay {
      # With sourcePreference you have a choice to make:
      # Prefer downloading packages as binary wheels.
      sourcePreference = "wheel";
      # Prefer building packages from source.
      # sourcePreference = "sdist";
    };
    editableOverlay = workspace.mkEditablePyprojectOverlay {
      # Use environment variable pointing to editable root directory
      root = "$REPO_ROOT";
      # Optional: Only enable editable for these packages
      # members = [ "scufris" ];
    };
  in
    flake-parts.lib.mkFlake {inherit inputs;} {
      imports = [
        # To import an internal flake module: ./other.nix
        # To import an external flake module:
        #   1. Add foo to inputs
        #   2. Add foo as a parameter to the outputs function
        #   3. Add here: foo.flakeModule
      ];
      systems = ["x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin"];
      perSystem = {
        config,
        self',
        inputs',
        pkgs,
        system,
        ...
      }: let
        python = pkgs.python3;
        # Uv2nix uses pyproject.nix Python builders which needs to be instantiated with a nixpkgs instance:
        pythonBase = pkgs.callPackage pyproject-nix.build.packages {
          inherit python;
        };
        # Compose the Python base set + build systems + uv.lock generated packages into a concrete Python set:
        pythonSet = pythonBase.overrideScope (
          nixpkgs.lib.composeManyExtensions [
            # The build system overlay has the same sdist/wheel distinction as mkPyprojectOverlay:
            # Prefer build systems from binary wheels
            pyproject-build-systems.overlays.wheel
            # Prefer build systems packages from source
            # pyproject-build-systems.overlays.sdist
            overlay
          ]
        );
        # virtualenv = pythonSet.mkVirtualEnv "scufris-dev-env" workspace.deps.all;
        # Uv2nix supports editable packages, but requires you to generate a separate overlay & package set for them:
        editablePythonSet = pythonSet.overrideScope editableOverlay;
        virtualenv = editablePythonSet.mkVirtualEnv "scufris-dev-env" workspace.deps.all;
        inherit (pkgs.callPackages pyproject-nix.build.util {}) mkApplication;

        # Runtime venv — only the project's `default` deps, no dev tools.
        # Both the server and CLI are exposed as console scripts from the
        # same `scufris-bot` distribution, so a single mkApplication call
        # produces a `bin/` directory containing all three entrypoints
        # (`scufris-server`, `scufris-cli`, `scufris-bot`).
        runtimeVenv = pythonSet.mkVirtualEnv "scufris-env" workspace.deps.default;
        scufrisApp = mkApplication {
          venv = runtimeVenv;
          package = pythonSet.scufris-bot;
        };

        # Helper: a derivation that runs a single command against a
        # writable copy of the source tree using the dev venv. The
        # output is a marker file so `nix flake check` is happy.
        mkCheck = name: command:
          pkgs.runCommand "scufris-${name}" {
            nativeBuildInputs = [virtualenv pkgs.git pkgs.cacert];
            src = ./.;
          } ''
            cp -r $src work
            chmod -R +w work
            cd work
            export HOME=$TMPDIR
            export PYTHONPATH=
            export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
            export NIX_SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
            ${command}
            touch $out
          '';
      in {
        # Per-system attributes can be defined here. The self' and inputs'
        # module parameters provide easy access to attributes of the same
        # system.
        _module.args.pkgs = import self.inputs.nixpkgs {
          inherit system;
          config.allowUnfree = true;
          config.cudaSupport.enable = true;
        };

        # `scufris-server` and `scufris-cli` resolve to the same
        # underlying derivation today (one Python distribution, multiple
        # console scripts) but are exposed under distinct attribute
        # names so:
        #   * `nix run .#scufris-cli` picks the right `bin/` entry via
        #     `meta.mainProgram`.
        #   * downstream NixOS / Home Manager modules can depend on
        #     just one of them, leaving room for the two derivations to
        #     diverge later (e.g. server-only deps).
        packages = {
          scufris-server = scufrisApp.overrideAttrs (old: {
            meta = (old.meta or {}) // {mainProgram = "scufris-server";};
          });
          scufris-cli = scufrisApp.overrideAttrs (old: {
            meta = (old.meta or {}) // {mainProgram = "scufris-cli";};
          });
          scufris-bot = scufrisApp.overrideAttrs (old: {
            meta = (old.meta or {}) // {mainProgram = "scufris-bot";};
          });
          default = scufrisApp.overrideAttrs (old: {
            meta = (old.meta or {}) // {mainProgram = "scufris-server";};
          });
        };

        apps = {
          scufris-server = {
            type = "app";
            program = "${self.packages.${system}.scufris-server}/bin/scufris-server";
          };
          scufris-cli = {
            type = "app";
            program = "${self.packages.${system}.scufris-cli}/bin/scufris-cli";
          };
          scufris-bot = {
            type = "app";
            program = "${self.packages.${system}.scufris-bot}/bin/scufris-bot";
          };
          default = self'.apps.scufris-server;
        };

        # `nix flake check` runs the full QA gate. Each derivation
        # operates on a fresh writable copy of the source so caches
        # (mypy, pytest) can land in $TMPDIR rather than /nix/store.
        checks = {
          ruff = mkCheck "ruff" "ruff check .";
          mypy = mkCheck "mypy" "mypy .";
          pytest = mkCheck "pytest" "pytest";
        };

        devShells.default = pkgs.mkShell {
          packages = [
            virtualenv
            pkgs.uv
          ];
          env = {
            # Prevent uv from managing a virtual environment, this is managed by uv2nix.
            UV_NO_SYNC = "1";
            # Use interpreter path for all uv operations.
            # UV_PYTHON = pythonSet.python.interpreter;
            UV_PYTHON = editablePythonSet.python.interpreter;
            # Prevent uv from downloading managed Python interpreters, we use Nix instead.
            UV_PYTHON_DOWNLOADS = "never";
          };
          shellHook = ''
            # Unset to eliminate bad side effects from Nixpkgs Python builders.
            unset PYTHONPATH
            # To inform the virtualenv which directory editable packages are relative to.
            export REPO_ROOT=$(git rev-parse --show-toplevel)
            export LD_LIBRARY_PATH=${pkgs.libopus}/lib:${pkgs.ffmpeg}/lib:${pkgs.gcc.cc.lib}/lib:$LD_LIBRARY_PATH
            source ${virtualenv}/bin/activate
          '';
        };
      };
      flake = {
        # The usual flake attributes can be defined here, including system-
        # agnostic ones like nixosModule and system-enumerating ones, although
        # those are more easily expressed in perSystem.

        homeManagerModules.default = {config, lib, pkgs, ...}: let
          cfg = config.services.scufris-bot;
        in {
          options.services.scufris-bot = {
            enable = lib.mkEnableOption "Scufris Bot - Telegram bot with AI agent";

            environmentFile = lib.mkOption {
              type = lib.types.nullOr lib.types.path;
              default = null;
              example = "\${config.home.homeDirectory}/personal/scufris-bot/.env";
              description = ''
                Environment file containing secrets (TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, etc).
                This file should contain KEY=value pairs, one per line.
              '';
            };

            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.system}.default;
              description = "The scufris-bot package to use.";
            };
          };

          config = lib.mkIf cfg.enable {
            systemd.user.services.scufris-bot = {
              Unit = {
                Description = "Scufris Bot - Telegram bot with AI agent";
                After = [ "network-online.target" ];
              };

              Service = {
                Type = "simple";
                WorkingDirectory = config.home.homeDirectory;
                ExecStart = "${cfg.package}/bin/scufris-bot";
                Restart = "always";
                RestartSec = "10s";

                # Load environment variables from file
                EnvironmentFile = lib.mkIf (cfg.environmentFile != null) cfg.environmentFile;
              };

              Install = {
                WantedBy = [ "default.target" ];
              };
            };
          };
        };
      };
    };
}

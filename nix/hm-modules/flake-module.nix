{ self', ... }: {
  imports = [ ./scufris.nix ];
  config.homeManagerModules.scufris = import ./scufris.nix {
    package = self'.packages.scufris-server;
  };
}

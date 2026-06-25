{ self', ... }: {
  imports = [ ./scufris.nix ];
  config.nixosModules.scufris = import ./scufris.nix {
    package = self'.packages.scufris-server;
  };
}

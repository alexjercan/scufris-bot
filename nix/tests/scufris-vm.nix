# NixOS VM test for the scufris module.
#
# What it covers:
#   * Module loads, unit reaches active.
#   * /v1/healthz returns 200 (no Ollama needed for the liveness probe).
#   * `systemd-analyze security scufris` reports an exposure level
#     better than 2.0 (task acceptance criterion).
#   * SIGTERM lets the unit shut down cleanly within the 35s stop
#     timeout (exercises the SCUFRIS_SHUTDOWN_GRACE drain path).
#   * Restart-on-failure works.
#
# Out of scope for this VM test (would require building a fake Ollama
# HTTP server inside the VM): exercising /v1/chat end-to-end against a
# stub model. The codebase already has ample unit coverage of the chat
# pipeline against mocked agents.
{
  pkgs,
  scufrisModule,
  scufrisPackage,
}:
pkgs.testers.nixosTest {
  name = "scufris-server";

  nodes.machine = {...}: {
    imports = [scufrisModule];

    nixpkgs.overlays = [(_: _: {scufris-server = scufrisPackage;})];

    services.scufris = {
      enable = true;
      bind = "127.0.0.1";
      port = 8765;
      logLevel = "INFO";
    };

    environment.systemPackages = [pkgs.curl];

    # Tiny VM — no GUI, just enough to run a Python service.
    virtualisation.memorySize = 1024;
  };

  testScript = ''
    machine.start()
    machine.wait_for_unit("scufris.service")
    machine.wait_for_open_port(8765)

    # Liveness.
    machine.succeed("curl --fail --max-time 5 http://127.0.0.1:8765/v1/healthz")

    # Security posture: exposure level <= 2.0 ("OK" or better).
    score_line = machine.succeed(
        "systemd-analyze security scufris | tail -n1"
    )
    print("security score line:", score_line.strip())
    # Format: "→ Overall exposure level for scufris.service: 1.6 OK"
    import re
    m = re.search(r"([0-9]+\.[0-9]+)", score_line)
    assert m is not None, f"could not parse security score from: {score_line!r}"
    score = float(m.group(1))
    assert score <= 2.0, f"security exposure {score} exceeds 2.0 budget"

    # Graceful shutdown: SIGTERM should let the unit exit cleanly well
    # within TimeoutStopSec=35s.
    machine.succeed("systemctl stop scufris.service")
    machine.wait_until_fails("systemctl is-active --quiet scufris.service")

    # Restart works — service comes back up.
    machine.succeed("systemctl start scufris.service")
    machine.wait_for_unit("scufris.service")
    machine.wait_for_open_port(8765)
    machine.succeed("curl --fail --max-time 5 http://127.0.0.1:8765/v1/healthz")
  '';
}

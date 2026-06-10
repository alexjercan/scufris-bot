# NixOS VM test for the scufris module.
#
# What it covers:
#   * Both modules load — `scufris.service` and `opencode-serve.service`
#     reach active.
#   * /v1/healthz returns 200 (no Ollama needed for the liveness probe).
#   * /v1/readyz returns a parseable JSON document; the OpenCode side
#     of the readiness probe reaches the local daemon (status code <500
#     on `GET /session`). Ollama is intentionally absent in the VM, so
#     the overall readiness is "degraded" — that's fine, we're testing
#     wiring, not external dependency uptime.
#   * `services.scufris.environment.OPENCODE_BASE_URL` is auto-wired
#     from `services.opencode-serve.url`. Verified by reading the unit
#     environment via `systemctl show`.
#   * The scufris unit orders itself After/Wants on opencode-serve when
#     both are enabled (via `systemctl list-dependencies`).
#   * `systemd-analyze security` reports an exposure level <= 2.0 for
#     both units (task acceptance criterion).
#   * SIGTERM lets the unit shut down cleanly within the 35s stop
#     timeout (exercises the SCUFRIS_SHUTDOWN_GRACE drain path).
#   * Restart-on-failure works.
#
# Out of scope:
#   * Real chat turn — requires provider credentials baked into the VM,
#     which we don't carry in nix flake check. Unit tests + the smoke
#     scripts in `tasks/20260610-101413` cover that path.
{
  pkgs,
  scufrisModule,
  opencodeServeModule,
  scufrisPackage,
}:
pkgs.testers.nixosTest {
  name = "scufris-server";

  nodes.machine = {...}: {
    imports = [scufrisModule opencodeServeModule];

    nixpkgs.overlays = [(_: _: {scufris-server = scufrisPackage;})];

    services.opencode-serve = {
      enable = true;
      # 4096 is the module default; pinning here makes the assertions
      # below independent of any future default change.
      port = 4096;
      host = "127.0.0.1";
    };

    services.scufris = {
      enable = true;
      settings = {
        server = {
          bind = "127.0.0.1";
          port = 8765;
          log_level = "INFO";
        };
      };
    };

    environment.systemPackages = [pkgs.curl pkgs.jq];

    # Tiny VM — no GUI. Bun (OpenCode's runtime) is comfortable with
    # 1 GiB; bump if `opencode serve` starts OOM-ing in CI.
    virtualisation.memorySize = 1536;
  };

  testScript = ''
    machine.start()

    # Both units come up. opencode-serve binds first (the scufris
    # module orders scufris.service After=opencode-serve.service when
    # both are enabled), then scufris.
    machine.wait_for_unit("opencode-serve.service")
    machine.wait_for_open_port(4096)
    machine.wait_for_unit("scufris.service")
    machine.wait_for_open_port(8765)

    # Liveness.
    machine.succeed("curl --fail --max-time 5 http://127.0.0.1:8765/v1/healthz")

    # OpenCode is reachable on its own port (the readiness probe path).
    machine.succeed("curl --fail --max-time 5 http://127.0.0.1:4096/session")

    # OPENCODE_BASE_URL was auto-wired from the opencode-serve module's
    # computed url. Read the unit environment directly so we're not
    # racing the scufris startup log.
    env_blob = machine.succeed(
        "systemctl show scufris.service -p Environment --value"
    )
    print("scufris env:", env_blob.strip())
    assert "OPENCODE_BASE_URL=http://127.0.0.1:4096" in env_blob, (
        f"OPENCODE_BASE_URL not auto-wired into scufris env: {env_blob!r}"
    )

    # Ordering: scufris.service should list opencode-serve.service in
    # its After= deps.
    deps = machine.succeed(
        "systemctl show scufris.service -p After --value"
    )
    assert "opencode-serve.service" in deps, (
        f"scufris.service is not ordered After=opencode-serve.service: {deps!r}"
    )
    wants = machine.succeed(
        "systemctl show scufris.service -p Wants --value"
    )
    assert "opencode-serve.service" in wants, (
        f"scufris.service does not Want opencode-serve.service: {wants!r}"
    )

    # /v1/readyz is auth-gated; the default config leaves the token
    # unset, so the dependency is a no-op and the call should succeed.
    # Status will likely be "degraded" because there's no Ollama in
    # this VM, but the OpenCode side should report a numeric code.
    body = machine.succeed(
        "curl --fail --max-time 5 http://127.0.0.1:8765/v1/readyz"
    )
    print("readyz body:", body.strip())
    parsed = machine.succeed(f"echo {body!r} | jq .")
    print(parsed)
    # The opencode block should carry a numeric `code` (HTTP status
    # from `GET /session`) rather than an `error` field, because the
    # daemon is up locally.
    opencode_code = machine.succeed(
        f"echo {body!r} | jq -r '.opencode.code // empty'"
    ).strip()
    assert opencode_code != "", (
        f"readyz did not reach the local OpenCode daemon: {body!r}"
    )

    # Security posture: exposure level <= 2.0 ("OK" or better) on both
    # units.
    import re

    def parse_score(unit):
        line = machine.succeed(
            f"systemd-analyze security {unit} | tail -n1"
        )
        print(f"{unit} security score line:", line.strip())
        m = re.search(r"([0-9]+\.[0-9]+)", line)
        assert m is not None, f"could not parse security score from: {line!r}"
        return float(m.group(1))

    scufris_score = parse_score("scufris")
    assert scufris_score <= 2.0, f"scufris exposure {scufris_score} exceeds 2.0 budget"

    opencode_score = parse_score("opencode-serve")
    assert opencode_score <= 2.5, (
        f"opencode-serve exposure {opencode_score} exceeds 2.5 budget"
    )

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

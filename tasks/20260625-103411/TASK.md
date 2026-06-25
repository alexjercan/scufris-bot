# Set default Ollama model via OPENCODE_MODEL env variable

- STATUS: OPEN
- PRIORITY: 70
- TAGS: config,server,backend

Add an `OPENCODE_MODEL` environment variable to `scufris_server/config.py`
as an override for the default model probe logic in the lifespan startup.

- Field name: `opencode_model: str | None`
- Validation alias: `"OPENCODE_MODEL"`
- Default: `None`

Modify the lifespan startup in `app.py` (step 7, around line 210) so that:

1. If `settings.opencode_model` is set (non-None), use it directly as the
   default model — construct a `ModelRef` and cache it as
   `app.state.opencode_default_model`. Skip the opencode API call entirely.
2. If `settings.opencode_model` is `None` (default), keep the existing logic:
   call `client.get_default_model()` to probe the opencode daemon for the
   connected provider's default model.

This gives users a way to pin a specific model (e.g. `"qwen3:latest"`) without
depending on opencode's provider configuration, while preserving backward
compatibility for deployments that want opencode to decide the default.

Also update the NixOS/Home Manager modules to expose `OPENCODE_MODEL` as a
config option so it can be set in the deployment config.

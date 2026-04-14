# Add systemd daemon for the scufris bot in the nix flake

- STATUS: CLOSED
- PRIORITY: 50
- TAGS: feature, devops

---

## Goal

Create a home-manager systemd user service for the Scufris Bot that:
- Runs as a user service (not system-wide)
- Automatically starts on user login
- Restarts on failure
- Loads environment variables from a secure location
- Uses the proper Python environment from the flake

## Implementation Plan

### 1. Create Home-Manager Module
- [x] Add a `homeManagerModules.default` output to the flake
- [x] Define systemd user service configuration
- [x] Support configuration options (enable/disable, environment file path)

### 2. Service Configuration
- [x] Service type: `simple`
- [x] Restart policy: `always`
- [x] User service: runs under the home-manager user
- [x] Working directory: `$HOME` (fixed, not configurable)

### 3. Required Environment Variables (from .env.example)
- [x] `TELEGRAM_BOT_TOKEN` - Telegram bot API token
- [x] `ALLOWED_USER_IDS` - Comma-separated list of allowed user IDs
- [x] `OLLAMA_MODEL` - Ollama model name (default: qwen3:latest)
- [x] `OLLAMA_BASE_URL` - Ollama API URL (default: http://localhost:11434)

### 4. Integration
- [x] The service should use the `packages.default` output from the flake
- [x] Should be importable in home-manager configuration as: `inputs.scufris-bot.homeManagerModules.default`
- [x] Added script entry point to pyproject.toml (scufris-bot = "main:main")

## Files Modified

- `flake.nix` - Added homeManagerModules output with systemd user service definition, fixed app binary name
- `pyproject.toml` - Added [project.scripts] entry point for scufris-bot

## Usage After Implementation

```nix
# In home-manager configuration (e.g., home.nix or flake.nix)
{
  inputs.scufris-bot.url = "path:/home/alex/personal/scufris-bot";

  # In home-manager modules:
  imports = [ inputs.scufris-bot.homeManagerModules.default ];

  services.scufris-bot = {
    enable = true;
    environmentFile = "${config.home.homeDirectory}/personal/scufris-bot/.env";
  };
}
```

Note: The working directory is always `$HOME` and cannot be changed.

## Testing

After implementation, test with:
```bash
# Rebuild home-manager configuration
home-manager switch --flake .

# Check service status (user service)
systemctl --user status scufris-bot

# View logs
journalctl --user -u scufris-bot -f

# Restart service
systemctl --user restart scufris-bot

# Enable to start on login (done automatically by home-manager)
systemctl --user enable scufris-bot
```

"""Process-wide configuration loaded from environment variables.

Step 2 of #9. Defaults match design §6: opencode on loopback at port 4096,
scufris-server on loopback at port 7080, state under ``$XDG_STATE_HOME``.

Field names are short (``settings.bind``, ``settings.port``) but each is
bound to its uppercase env var via ``validation_alias``. ``populate_by_name``
is enabled so unit tests can construct ``Settings(bind=..., port=...)``
directly without going through the env.
"""

from __future__ import annotations

import os
import warnings
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hosts treated as loopback for the "no password but remote opencode"
# safety check. Empty string covers ``urlparse("http:///path").hostname``
# returning ``None`` → coerced to "" before lookup.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


def _default_state_dir() -> Path:
    """Resolve the default state directory.

    Honors ``$XDG_STATE_HOME`` when set (XDG Base Directory spec), else
    falls back to ``~/.local/state``. Always appends the ``scufris``
    leaf, regardless of which base was chosen.
    """
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "scufris"


def _is_loopback_url(url: str) -> bool:
    """True iff ``url`` points at the local machine."""
    try:
        host = urlparse(url).hostname
    except (ValueError, TypeError):
        return False
    return (host or "") in _LOOPBACK_HOSTS


class Settings(BaseSettings):
    """scufris-server runtime configuration.

    One instance per process — access via :func:`get_settings`. Env vars
    are read at instantiation time; subsequent env mutations require
    ``get_settings.cache_clear()`` to take effect.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    bind: str = Field(
        default="127.0.0.1",
        validation_alias="SCUFRIS_BIND",
        description="Interface scufris-server binds to.",
    )
    port: int = Field(
        default=7080,
        ge=1,
        le=65535,
        validation_alias="SCUFRIS_PORT",
        description="TCP port scufris-server listens on.",
    )
    opencode_url: str = Field(
        default="http://127.0.0.1:4096",
        validation_alias="OPENCODE_URL",
        description="Base URL of the opencode serve daemon.",
    )
    opencode_password: str | None = Field(
        default=None,
        validation_alias="OPENCODE_SERVER_PASSWORD",
        description=(
            "Bearer token expected by opencode. Optional for loopback; "
            "warned about at startup if unset for a remote URL."
        ),
    )
    state_dir: Path = Field(
        default_factory=_default_state_dir,
        validation_alias="SCUFRIS_STATE_DIR",
        description="Directory for SQLite DB and other persistent state.",
    )
    user_id: int | None = Field(
        default=None,
        ge=1,
        validation_alias="SCUFRIS_USER_ID",
        description=(
            "Server-side identity override (#12). When set, every call to "
            "``resolve_user`` short-circuits to this user_id, bypassing the "
            "TOML lookup and skipping any ``surface_bindings`` insert. "
            "Useful for single-user dev/test deployments and for the v1 "
            "carryover where the bot pinned itself to a known id."
        ),
    )
    config_path: Path | None = Field(
        default=None,
        validation_alias="SCUFRIS_CONFIG",
        description=(
            "Explicit path to ``config.toml`` (#12). When ``None`` (the "
            "default), :func:`scufris_server.identity.load_user_identity` "
            "resolves ``$XDG_CONFIG_HOME/scufris/config.toml`` (falling "
            "back to ``~/.config/scufris/config.toml``). When set, that "
            "exact path is used. A non-existent path is *not* an error — "
            "the loader treats it as 'no user defined' and only the "
            "default user (id=1) will resolve."
        ),
    )
    opencode_model: str | None = Field(
        default=None,
        validation_alias="OPENCODE_MODEL",
        description=(
            "Override the default model probed from the opencode daemon "
            "(step 7 of #9). When set, ``app.py`` constructs a "
            ":class:`~scufris_server.opencode_client.ModelRef` directly "
            "from ``providerID/modelID`` (``modelID`` alone falls back "
            "to ``ollama``) and caches it as "
            "``app.state.opencode_default_model``, skipping the "
            "``GET /provider`` probe entirely. When ``None`` the "
            "existing probe logic is retained for backward compatibility."
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` instance.

    Cached after the first call; tests that mutate env vars must call
    ``get_settings.cache_clear()`` to force a re-read.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        dotenv.load_dotenv()

    return Settings()


def warn_unsafe_settings(settings: Settings) -> None:
    """Emit a :class:`UserWarning` if the runtime config is insecure.

    Currently triggers when ``opencode_password`` is unset *and* the
    ``opencode_url`` is non-loopback — i.e. unauthenticated requests
    would fly across the network. Called from app startup (step 5).
    """
    if settings.opencode_password is None and not _is_loopback_url(
        settings.opencode_url
    ):
        warnings.warn(
            "OPENCODE_SERVER_PASSWORD is unset but OPENCODE_URL "
            f"({settings.opencode_url!r}) is non-loopback. "
            "Requests to opencode will be unauthenticated.",
            UserWarning,
            stacklevel=2,
        )

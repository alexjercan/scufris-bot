"""Unified configuration for Scufris.

Single source of truth for both server and front-ends (CLI, Telegram bot).
Schema is TOML-first; environment variables are an *override* layer for
ops convenience and for secrets that mustn't end up in the Nix store.

Lookup order for the config file (first existing path wins; missing file
is OK and yields all-defaults):

  1. ``$SCUFRIS_CONFIG``  — explicit override, useful for tests
  2. ``$XDG_CONFIG_HOME/scufris/config.toml``
  3. ``~/.config/scufris/config.toml``

Schema (every section is optional; only ``[user]`` and ``[telegram]``
have any required fields, and only when the caller asks for them):

.. code-block:: toml

    [user]
    username = "alex"          # canonical name; hashed into the wire user_id
    timezone = "Europe/Berlin"

    [user.identity]
    # surface_id values that resolve to ``username``. Telegram ids are
    # written as bare integers; CLI uses whatever the surface_id string
    # is (typically ``$SCUFRIS_USER`` or ``getpass.getuser()``).
    telegram = 8231376426
    cli      = "alex"

    [user.journal]
    den_path = "~/the-den"     # parsed; not yet wired into journal tools

    [telegram]
    # bot_token = "..."        # SECRET — leave in env (TELEGRAM_BOT_TOKEN)
    allowed_user_ids = [8231376426]

    [ollama]
    model       = "qwen3:latest"
    base_url    = "http://localhost:11434"
    temperature = 0.7
    reasoning   = true

    [history]
    max_per_user = 20

    [server]
    bind           = "127.0.0.1"
    port           = 8765
    log_level      = "INFO"
    shutdown_grace = 30
    # token = "..."            # SECRET — leave in env (SCUFRIS_TOKEN)

    [client]
    server_url    = "http://127.0.0.1:8765"
    full_thinking = true

Env-var override map (env wins when both are set):

==========================  ===============================
Env var                     TOML path
==========================  ===============================
``TELEGRAM_BOT_TOKEN``      ``telegram.bot_token``
``ALLOWED_USER_IDS``        ``telegram.allowed_user_ids``
``OLLAMA_MODEL``            ``ollama.model``
``OLLAMA_BASE_URL``         ``ollama.base_url``
``OLLAMA_TEMPERATURE``      ``ollama.temperature``
``OLLAMA_REASONING``        ``ollama.reasoning``
``MAX_HISTORY_PER_USER``    ``history.max_per_user``
``SCUFRIS_BIND``            ``server.bind``
``SCUFRIS_PORT``            ``server.port``
``SCUFRIS_LOG_LEVEL``       ``server.log_level``
``SCUFRIS_TOKEN``           ``server.token``
``SCUFRIS_SHUTDOWN_GRACE``  ``server.shutdown_grace``
``SCUFRIS_SERVER_URL``      ``client.server_url``
``SCUFRIS_FULL_THINKING``   ``client.full_thinking``
==========================  ===============================

Other env vars (``SCUFRIS_USER``, ``SCUFRIS_USER_ID``,
``SCUFRIS_TELEMETRY``, ``SCUFRIS_COMPACTOR{,_MODEL}``,
``SCUFRIS_CONFIG``) intentionally stay env-only — they're either
per-invocation overrides (the first three) or debugging knobs.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import tomllib
from dotenv import load_dotenv

logger = logging.getLogger("scufris-bot.config")


# Top-level keys we recognise. Anything else triggers a warning so typos
# surface at startup rather than silently doing nothing.
_KNOWN_TOP_LEVEL = frozenset(
    {"user", "telegram", "ollama", "history", "server", "client"}
)
_KNOWN_USER_KEYS = frozenset({"username", "timezone", "identity", "journal"})
_KNOWN_TELEGRAM_KEYS = frozenset({"bot_token", "allowed_user_ids"})
_KNOWN_OLLAMA_KEYS = frozenset({"model", "base_url", "temperature", "reasoning"})
_KNOWN_HISTORY_KEYS = frozenset({"max_per_user"})
_KNOWN_SERVER_KEYS = frozenset({"bind", "port", "log_level", "token", "shutdown_grace"})
_KNOWN_CLIENT_KEYS = frozenset({"server_url", "full_thinking"})
_KNOWN_JOURNAL_KEYS = frozenset({"den_path"})


# =====================================================================
# Schema
# =====================================================================


@dataclass(frozen=True)
class UserJournal:
    """``[user.journal]`` — journal/den configuration. Currently parsed
    but not yet plumbed into the journal tools (follow-up task)."""

    den_path: Optional[str] = None


@dataclass(frozen=True)
class UserIdentity:
    """``[user.identity]`` — surface → surface_id mapping.

    A user owns at most one identity per surface in v1. Values are
    coerced to strings on parse so callers get a uniform type even when
    a Telegram id is written as an integer in TOML.
    """

    bindings: Mapping[str, str] = field(default_factory=dict)

    def matches(self, surface: str, surface_id: str) -> bool:
        bound = self.bindings.get(surface)
        return bound is not None and bound == surface_id


@dataclass(frozen=True)
class UserSection:
    """``[user]`` — single-user section. Multi-user is a future task."""

    username: Optional[str] = None
    timezone: Optional[str] = None
    identity: UserIdentity = field(default_factory=UserIdentity)
    journal: UserJournal = field(default_factory=UserJournal)


@dataclass(frozen=True)
class TelegramSection:
    """``[telegram]`` — Telegram bot credentials and ACL."""

    bot_token: Optional[str] = None
    allowed_user_ids: Tuple[int, ...] = ()


@dataclass(frozen=True)
class OllamaSection:
    """``[ollama]`` — model backend selection."""

    model: str = "qwen3:latest"
    base_url: str = "http://localhost:11434"
    temperature: float = 0.7
    reasoning: bool = True


@dataclass(frozen=True)
class HistorySection:
    """``[history]`` — chat history limits."""

    max_per_user: int = 20


@dataclass(frozen=True)
class ServerSection:
    """``[server]`` — daemon listen + auth + lifecycle."""

    bind: str = "127.0.0.1"
    port: int = 8765
    log_level: str = "INFO"
    token: Optional[str] = None
    shutdown_grace: float = 30.0


@dataclass(frozen=True)
class ClientSection:
    """``[client]`` — front-end (CLI / bot) defaults."""

    server_url: str = "http://127.0.0.1:8765"
    full_thinking: bool = True


@dataclass(frozen=True)
class Config:
    """Unified Scufris configuration.

    Construct via :func:`load_config`. ``source_path`` is ``None`` when
    no config file was found — callers can render a "tried these paths"
    diagnostic by calling :func:`config_search_paths`.
    """

    user: UserSection = field(default_factory=UserSection)
    telegram: TelegramSection = field(default_factory=TelegramSection)
    ollama: OllamaSection = field(default_factory=OllamaSection)
    history: HistorySection = field(default_factory=HistorySection)
    server: ServerSection = field(default_factory=ServerSection)
    client: ClientSection = field(default_factory=ClientSection)
    source_path: Optional[Path] = None


@dataclass(frozen=True)
class ResolvedIdentity:
    """Result of mapping a (surface, surface_id) to a canonical user."""

    user_id: int
    username: Optional[str]


# =====================================================================
# Path lookup
# =====================================================================


def config_search_paths() -> List[Path]:
    """Return candidate config paths in lookup order.

    Always returns at least one path even when env vars are unset, so
    callers can render a useful "tried these" message.
    """
    paths: List[Path] = []
    explicit = os.environ.get("SCUFRIS_CONFIG")
    if explicit:
        paths.append(Path(explicit).expanduser())
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        paths.append(Path(xdg).expanduser() / "scufris" / "config.toml")
    paths.append(Path.home() / ".config" / "scufris" / "config.toml")

    seen: set[Path] = set()
    deduped: List[Path] = []
    for p in paths:
        ap = p.resolve() if p.exists() else p
        if ap in seen:
            continue
        seen.add(ap)
        deduped.append(p)
    return deduped


# =====================================================================
# Parsing helpers
# =====================================================================


def _warn_unknown(scope: str, found: set[str], known: frozenset[str]) -> None:
    extras = sorted(found - known)
    if extras:
        logger.warning(
            "%s: ignoring unknown key(s) %s — typo or unwired feature?",
            scope,
            ", ".join(extras),
        )


def _require_str(scope: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{scope} must be a string, got {type(value).__name__}")
    return value


def _require_int(scope: str, value: Any) -> int:
    # bool is a subclass of int — reject so e.g. ``port = true`` doesn't
    # silently become 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{scope} must be an integer, got {type(value).__name__}")
    return value


def _require_number(scope: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{scope} must be a number, got {type(value).__name__}")
    return float(value)


def _require_bool(scope: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{scope} must be a boolean, got {type(value).__name__}")
    return value


def _parse_user_identity(raw: Any) -> UserIdentity:
    if raw is None:
        return UserIdentity()
    if not isinstance(raw, dict):
        raise ValueError("[user.identity] must be a table")
    bindings: Dict[str, str] = {}
    for surface, surface_id in raw.items():
        if not isinstance(surface, str) or not surface:
            raise ValueError("[user.identity] keys must be non-empty strings")
        if isinstance(surface_id, bool) or not isinstance(surface_id, (int, str)):
            raise ValueError(
                f"[user.identity].{surface} must be a string or integer, "
                f"got {type(surface_id).__name__}"
            )
        bindings[surface] = str(surface_id)
    return UserIdentity(bindings=bindings)


def _parse_user_journal(raw: Any) -> UserJournal:
    if raw is None:
        return UserJournal()
    if not isinstance(raw, dict):
        raise ValueError("[user.journal] must be a table")
    _warn_unknown("[user.journal]", set(raw), _KNOWN_JOURNAL_KEYS)
    den_path = raw.get("den_path")
    if den_path is not None:
        den_path = os.path.expanduser(_require_str("[user.journal].den_path", den_path))
    return UserJournal(den_path=den_path)


def _parse_user(raw: Any) -> UserSection:
    if raw is None:
        return UserSection()
    if not isinstance(raw, dict):
        raise ValueError("[user] must be a table")
    _warn_unknown("[user]", set(raw), _KNOWN_USER_KEYS)
    username = raw.get("username")
    if username is not None:
        username = _require_str("[user].username", username)
    timezone = raw.get("timezone")
    if timezone is not None:
        timezone = _require_str("[user].timezone", timezone)
    return UserSection(
        username=username,
        timezone=timezone,
        identity=_parse_user_identity(raw.get("identity")),
        journal=_parse_user_journal(raw.get("journal")),
    )


def _parse_telegram(raw: Any) -> TelegramSection:
    if raw is None:
        return TelegramSection()
    if not isinstance(raw, dict):
        raise ValueError("[telegram] must be a table")
    _warn_unknown("[telegram]", set(raw), _KNOWN_TELEGRAM_KEYS)
    bot_token = raw.get("bot_token")
    if bot_token is not None:
        bot_token = _require_str("[telegram].bot_token", bot_token)
    raw_ids = raw.get("allowed_user_ids", [])
    if not isinstance(raw_ids, list):
        raise ValueError("[telegram].allowed_user_ids must be an array")
    ids: List[int] = []
    for i, val in enumerate(raw_ids):
        ids.append(_require_int(f"[telegram].allowed_user_ids[{i}]", val))
    return TelegramSection(bot_token=bot_token, allowed_user_ids=tuple(ids))


def _parse_ollama(raw: Any) -> OllamaSection:
    if raw is None:
        return OllamaSection()
    if not isinstance(raw, dict):
        raise ValueError("[ollama] must be a table")
    _warn_unknown("[ollama]", set(raw), _KNOWN_OLLAMA_KEYS)
    defaults = OllamaSection()
    return OllamaSection(
        model=_require_str("[ollama].model", raw.get("model", defaults.model)),
        base_url=_require_str(
            "[ollama].base_url", raw.get("base_url", defaults.base_url)
        ),
        temperature=_require_number(
            "[ollama].temperature", raw.get("temperature", defaults.temperature)
        ),
        reasoning=_require_bool(
            "[ollama].reasoning", raw.get("reasoning", defaults.reasoning)
        ),
    )


def _parse_history(raw: Any) -> HistorySection:
    if raw is None:
        return HistorySection()
    if not isinstance(raw, dict):
        raise ValueError("[history] must be a table")
    _warn_unknown("[history]", set(raw), _KNOWN_HISTORY_KEYS)
    defaults = HistorySection()
    return HistorySection(
        max_per_user=_require_int(
            "[history].max_per_user", raw.get("max_per_user", defaults.max_per_user)
        ),
    )


def _parse_server(raw: Any) -> ServerSection:
    if raw is None:
        return ServerSection()
    if not isinstance(raw, dict):
        raise ValueError("[server] must be a table")
    _warn_unknown("[server]", set(raw), _KNOWN_SERVER_KEYS)
    defaults = ServerSection()
    token = raw.get("token")
    if token is not None:
        token = _require_str("[server].token", token)
    return ServerSection(
        bind=_require_str("[server].bind", raw.get("bind", defaults.bind)),
        port=_require_int("[server].port", raw.get("port", defaults.port)),
        log_level=_require_str(
            "[server].log_level", raw.get("log_level", defaults.log_level)
        ),
        token=token,
        shutdown_grace=_require_number(
            "[server].shutdown_grace",
            raw.get("shutdown_grace", defaults.shutdown_grace),
        ),
    )


def _parse_client(raw: Any) -> ClientSection:
    if raw is None:
        return ClientSection()
    if not isinstance(raw, dict):
        raise ValueError("[client] must be a table")
    _warn_unknown("[client]", set(raw), _KNOWN_CLIENT_KEYS)
    defaults = ClientSection()
    return ClientSection(
        server_url=_require_str(
            "[client].server_url", raw.get("server_url", defaults.server_url)
        ),
        full_thinking=_require_bool(
            "[client].full_thinking",
            raw.get("full_thinking", defaults.full_thinking),
        ),
    )


def parse_config(raw: Mapping[str, Any], source: Optional[Path] = None) -> Config:
    """Validate a raw TOML mapping into a :class:`Config`.

    Raises :class:`ValueError` on schema violations (callers convert to
    a friendly message). Unknown *top-level* sections produce a warning
    but don't fail.
    """
    _warn_unknown("config.toml", set(raw), _KNOWN_TOP_LEVEL)
    return Config(
        user=_parse_user(raw.get("user")),
        telegram=_parse_telegram(raw.get("telegram")),
        ollama=_parse_ollama(raw.get("ollama")),
        history=_parse_history(raw.get("history")),
        server=_parse_server(raw.get("server")),
        client=_parse_client(raw.get("client")),
        source_path=source,
    )


# =====================================================================
# Env overrides
# =====================================================================


def _env_str(key: str) -> Optional[str]:
    val = os.environ.get(key)
    return val if val else None


def _env_bool(key: str) -> Optional[bool]:
    """Parse boolean env values: 1/0, true/false, yes/no, on/off (case-insensitive)."""
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return None
    norm = raw.strip().lower()
    if norm in ("1", "true", "yes", "on"):
        return True
    if norm in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{key}={raw!r}: expected a boolean (1/0, true/false, yes/no)")


def _env_int(key: str) -> Optional[int]:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key}={raw!r}: expected an integer") from exc


def _env_float(key: str) -> Optional[float]:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key}={raw!r}: expected a number") from exc


def _env_int_list(key: str) -> Optional[Tuple[int, ...]]:
    """Comma-separated integers — preserves the legacy ALLOWED_USER_IDS shape."""
    raw = os.environ.get(key)
    if raw is None:
        return None
    items: List[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            items.append(int(tok))
        except ValueError as exc:
            raise ValueError(
                f"{key}: cannot parse {tok!r} as integer in {raw!r}"
            ) from exc
    return tuple(items)


def _apply_env_overrides(cfg: Config) -> Config:
    """Layer env vars on top of TOML values. Env wins."""
    telegram = cfg.telegram
    bot_token = _env_str("TELEGRAM_BOT_TOKEN")
    allowed = _env_int_list("ALLOWED_USER_IDS")
    if bot_token is not None or allowed is not None:
        telegram = replace(
            telegram,
            bot_token=bot_token if bot_token is not None else telegram.bot_token,
            allowed_user_ids=allowed
            if allowed is not None
            else telegram.allowed_user_ids,
        )

    ollama = cfg.ollama
    o_model = _env_str("OLLAMA_MODEL")
    o_url = _env_str("OLLAMA_BASE_URL")
    o_temp = _env_float("OLLAMA_TEMPERATURE")
    o_reason = _env_bool("OLLAMA_REASONING")
    if any(v is not None for v in (o_model, o_url, o_temp, o_reason)):
        ollama = replace(
            ollama,
            model=o_model if o_model is not None else ollama.model,
            base_url=o_url if o_url is not None else ollama.base_url,
            temperature=o_temp if o_temp is not None else ollama.temperature,
            reasoning=o_reason if o_reason is not None else ollama.reasoning,
        )

    history = cfg.history
    h_max = _env_int("MAX_HISTORY_PER_USER")
    if h_max is not None:
        history = replace(history, max_per_user=h_max)

    server = cfg.server
    s_bind = _env_str("SCUFRIS_BIND")
    s_port = _env_int("SCUFRIS_PORT")
    s_log = _env_str("SCUFRIS_LOG_LEVEL")
    s_token = _env_str("SCUFRIS_TOKEN")
    s_grace = _env_float("SCUFRIS_SHUTDOWN_GRACE")
    if any(v is not None for v in (s_bind, s_port, s_log, s_token, s_grace)):
        server = replace(
            server,
            bind=s_bind if s_bind is not None else server.bind,
            port=s_port if s_port is not None else server.port,
            log_level=s_log if s_log is not None else server.log_level,
            token=s_token if s_token is not None else server.token,
            shutdown_grace=s_grace if s_grace is not None else server.shutdown_grace,
        )

    client = cfg.client
    c_url = _env_str("SCUFRIS_SERVER_URL")
    c_thinking = _env_bool("SCUFRIS_FULL_THINKING")
    if c_url is not None or c_thinking is not None:
        client = replace(
            client,
            server_url=c_url if c_url is not None else client.server_url,
            full_thinking=c_thinking
            if c_thinking is not None
            else client.full_thinking,
        )

    return replace(
        cfg,
        telegram=telegram,
        ollama=ollama,
        history=history,
        server=server,
        client=client,
    )


# =====================================================================
# Loading
# =====================================================================


def load_config(
    *,
    require_telegram: bool = False,
    explicit_path: Optional[Path] = None,
    use_dotenv: bool = True,
) -> Config:
    """Locate, read, parse, and env-override the unified config.

    Args:
        require_telegram: if True, validate that ``telegram.bot_token``
            and ``telegram.allowed_user_ids`` are populated. Used by the
            Telegram bot front-end; the CLI and server use False.
        explicit_path: skip the search and read this exact path.
        use_dotenv: load a ``.env`` file before reading env overrides.
            Disable in tests that want isolation.

    Never raises on "file missing"; only on "file present but malformed"
    or — when ``require_telegram=True`` — missing telegram credentials.
    """
    if use_dotenv:
        load_dotenv()

    paths = [explicit_path] if explicit_path is not None else config_search_paths()

    cfg: Config = Config()
    for path in paths:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path}: malformed TOML: {exc}") from exc
        try:
            cfg = parse_config(raw, source=path)
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
        logger.info("loaded config from %s", path)
        break
    else:
        logger.info(
            "no config file found (searched: %s); using defaults + env",
            ", ".join(str(p) for p in paths),
        )

    cfg = _apply_env_overrides(cfg)

    if require_telegram:
        if not cfg.telegram.bot_token:
            logger.critical("telegram.bot_token (or TELEGRAM_BOT_TOKEN) not set")
            raise ValueError(
                "telegram.bot_token not set: put it in [telegram] in your "
                "config.toml or export TELEGRAM_BOT_TOKEN"
            )
        if not cfg.telegram.allowed_user_ids:
            logger.critical("telegram.allowed_user_ids (or ALLOWED_USER_IDS) empty")
            raise ValueError(
                "telegram.allowed_user_ids is empty: list at least one id in "
                "[telegram] in your config.toml or export ALLOWED_USER_IDS"
            )

    _log_summary(cfg)
    return cfg


def _log_summary(cfg: Config) -> None:
    logger.info(
        "config: ollama=%s @ %s (temp=%s, reasoning=%s); history=%d/user",
        cfg.ollama.model,
        cfg.ollama.base_url,
        cfg.ollama.temperature,
        cfg.ollama.reasoning,
        cfg.history.max_per_user,
    )
    if cfg.telegram.allowed_user_ids:
        logger.info(
            "telegram allowed user ids: %s", list(cfg.telegram.allowed_user_ids)
        )


# =====================================================================
# Identity resolution
# =====================================================================


def _user_id_for(name: Optional[str] = None) -> int:
    """Lazy import wrapper to avoid the ``utils ↔ scufris_client`` cycle."""
    from scufris_client.client import user_id_for as _impl

    return _impl(name)


def resolve_user_id(
    surface: str,
    surface_id: str,
    config: Config,
) -> ResolvedIdentity:
    """Map a (surface, surface_id) to the canonical ``user_id`` integer.

    Resolution order:

      1. If the configured ``[user.identity]`` mapping binds this
         (surface, surface_id) to the configured ``[user].username``,
         return ``user_id_for(username)`` and the username.
      2. Else if surface_id parses as an int, return it as-is (preserves
         the Telegram bot's pre-config behavior of using raw numeric ids).
      3. Else hash the surface_id (preserves the CLI's pre-config
         ``user_id_for(getpass.getuser())`` behavior).
    """
    user = config.user
    if user.username and user.identity.matches(surface, surface_id):
        return ResolvedIdentity(
            user_id=_user_id_for(user.username),
            username=user.username,
        )

    try:
        as_int = int(surface_id)
    except (TypeError, ValueError):
        return ResolvedIdentity(user_id=_user_id_for(surface_id), username=None)
    return ResolvedIdentity(user_id=as_int, username=None)

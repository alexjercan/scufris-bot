"""Tests for :mod:`utils.config` — TOML schema, env overrides, path
lookup, and identity resolution. Pure-Python; no network."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scufris_client.client import user_id_for
from utils.config import (
    Config,
    ResolvedIdentity,
    UserIdentity,
    UserSection,
    config_search_paths,
    load_config,
    parse_config,
    resolve_user_id,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every Scufris-related env var so tests don't accidentally
    inherit values from the developer's shell. Tests that need a
    specific env var set will re-add it via ``monkeypatch.setenv``."""
    for key in (
        "SCUFRIS_CONFIG",
        "SCUFRIS_BIND",
        "SCUFRIS_PORT",
        "SCUFRIS_LOG_LEVEL",
        "SCUFRIS_TOKEN",
        "SCUFRIS_SHUTDOWN_GRACE",
        "SCUFRIS_SERVER_URL",
        "SCUFRIS_FULL_THINKING",
        "TELEGRAM_BOT_TOKEN",
        "ALLOWED_USER_IDS",
        "OLLAMA_MODEL",
        "OLLAMA_BASE_URL",
        "OLLAMA_TEMPERATURE",
        "OLLAMA_REASONING",
        "MAX_HISTORY_PER_USER",
    ):
        monkeypatch.delenv(key, raising=False)


# ----------------------------------------------------------------------
# parse_config — pure-TOML parsing
# ----------------------------------------------------------------------


def test_parse_empty_returns_defaults() -> None:
    cfg = parse_config({})
    assert cfg.user.username is None
    assert cfg.user.timezone is None
    assert cfg.user.identity.bindings == {}
    assert cfg.user.journal.den_path is None
    assert cfg.telegram.bot_token is None
    assert cfg.telegram.allowed_user_ids == ()
    assert cfg.ollama.model == "qwen3:latest"
    assert cfg.ollama.base_url == "http://localhost:11434"
    assert cfg.ollama.temperature == 0.7
    assert cfg.ollama.reasoning is True
    assert cfg.history.max_per_user == 20
    assert cfg.server.bind == "127.0.0.1"
    assert cfg.server.port == 8765
    assert cfg.server.token is None
    assert cfg.server.shutdown_grace == 30.0
    assert cfg.client.server_url == "http://127.0.0.1:8765"
    assert cfg.client.full_thinking is True
    assert cfg.source_path is None


def test_parse_full_config_round_trips_every_section() -> None:
    cfg = parse_config(
        {
            "user": {
                "username": "alex",
                "timezone": "Europe/Berlin",
                "identity": {"telegram": 8231376426, "cli": "alex"},
                "journal": {"den_path": "/tmp/den"},
            },
            "telegram": {"bot_token": "bot:fake", "allowed_user_ids": [42, 43]},
            "ollama": {
                "model": "qwen3:14b",
                "base_url": "http://ollama:11434",
                "temperature": 0.3,
                "reasoning": False,
            },
            "history": {"max_per_user": 50},
            "server": {
                "bind": "0.0.0.0",
                "port": 9000,
                "log_level": "DEBUG",
                "token": "shh",
                "shutdown_grace": 10,
            },
            "client": {
                "server_url": "http://remote:9000",
                "full_thinking": False,
            },
        }
    )
    assert cfg.user.username == "alex"
    assert cfg.user.identity.bindings == {
        "telegram": "8231376426",  # ints coerced to strings
        "cli": "alex",
    }
    assert cfg.telegram.bot_token == "bot:fake"
    assert cfg.telegram.allowed_user_ids == (42, 43)
    assert cfg.ollama.model == "qwen3:14b"
    assert cfg.ollama.temperature == 0.3
    assert cfg.ollama.reasoning is False
    assert cfg.history.max_per_user == 50
    assert cfg.server.bind == "0.0.0.0"
    assert cfg.server.token == "shh"
    assert cfg.server.shutdown_grace == 10.0
    assert cfg.client.server_url == "http://remote:9000"
    assert cfg.client.full_thinking is False


def test_parse_unknown_top_level_warns_not_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING")
    cfg = parse_config({"future_thing": {"x": 1}})
    assert cfg.user.username is None
    assert any("future_thing" in r.message for r in caplog.records)


def test_parse_invalid_user_table_raises() -> None:
    with pytest.raises(ValueError, match=r"\[user\] must be a table"):
        parse_config({"user": "alex"})


def test_parse_invalid_identity_value_raises() -> None:
    with pytest.raises(ValueError, match=r"\[user.identity\].telegram"):
        parse_config({"user": {"identity": {"telegram": [1, 2]}}})


def test_parse_invalid_server_port_type_raises() -> None:
    with pytest.raises(ValueError, match=r"\[server\].port must be an integer"):
        parse_config({"server": {"port": "9000"}})


def test_parse_rejects_bool_for_int_field() -> None:
    # bool is a subclass of int in Python — make sure we don't silently
    # accept ``port = true`` becoming 1.
    with pytest.raises(ValueError, match=r"\[server\].port must be an integer"):
        parse_config({"server": {"port": True}})


def test_parse_rejects_string_for_bool_field() -> None:
    with pytest.raises(ValueError, match=r"\[ollama\].reasoning must be a boolean"):
        parse_config({"ollama": {"reasoning": "yes"}})


def test_parse_rejects_non_int_in_allowed_user_ids() -> None:
    with pytest.raises(ValueError, match=r"\[telegram\].allowed_user_ids\[1\]"):
        parse_config({"telegram": {"allowed_user_ids": [1, "two"]}})


def test_user_journal_den_path_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/tmp/home")
    cfg = parse_config({"user": {"journal": {"den_path": "~/notes"}}})
    assert cfg.user.journal.den_path == "/tmp/home/notes"


# ----------------------------------------------------------------------
# config_search_paths / load_config (file lookup, no env overrides)
# ----------------------------------------------------------------------


def test_search_paths_uses_explicit_then_xdg_then_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SCUFRIS_CONFIG", str(tmp_path / "explicit.toml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    paths = config_search_paths()
    assert paths[0] == tmp_path / "explicit.toml"
    assert paths[1] == tmp_path / "xdg" / "scufris" / "config.toml"
    assert paths[-1] == tmp_path / "home" / ".config" / "scufris" / "config.toml"


def test_search_paths_dedupes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "scufris" / "config.toml"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path.parent))
    monkeypatch.setenv("SCUFRIS_CONFIG", str(target))
    paths = config_search_paths()
    assert paths.count(target) == 1


def test_load_returns_defaults_when_no_file_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SCUFRIS_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "home-empty"))
    cfg = load_config(use_dotenv=False)
    assert isinstance(cfg, Config)
    assert cfg.source_path is None
    assert cfg.user.username is None


def test_load_reads_first_existing_file(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            [user]
            username = "alex"
            timezone = "UTC"

            [user.identity]
            telegram = 42
            cli = "alex"

            [ollama]
            model = "qwen3:14b"
            """
        )
    )
    cfg = load_config(explicit_path=cfg_path, use_dotenv=False)
    assert cfg.source_path == cfg_path
    assert cfg.user.username == "alex"
    assert cfg.user.identity.bindings == {"telegram": "42", "cli": "alex"}
    assert cfg.ollama.model == "qwen3:14b"


def test_load_raises_on_malformed_toml(tmp_path: Path) -> None:
    cfg_path = tmp_path / "broken.toml"
    cfg_path.write_text("user = [oops")
    with pytest.raises(ValueError, match="malformed TOML"):
        load_config(explicit_path=cfg_path, use_dotenv=False)


def test_load_raises_when_require_telegram_and_token_missing(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="telegram.bot_token not set"):
        load_config(
            explicit_path=tmp_path / "missing.toml",
            require_telegram=True,
            use_dotenv=False,
        )


def test_load_raises_when_require_telegram_and_user_ids_empty(
    tmp_path: Path,
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[telegram]\nbot_token = "bot:fake"\n')
    with pytest.raises(ValueError, match="allowed_user_ids is empty"):
        load_config(
            explicit_path=cfg_path,
            require_telegram=True,
            use_dotenv=False,
        )


# ----------------------------------------------------------------------
# Env override layer
# ----------------------------------------------------------------------


def test_env_overrides_layer_on_top_of_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            [ollama]
            model    = "qwen3:14b"
            base_url = "http://from-toml:11434"

            [server]
            bind = "0.0.0.0"
            port = 9000

            [client]
            server_url    = "http://from-toml:9000"
            full_thinking = false
            """
        )
    )
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:32b")
    monkeypatch.setenv("SCUFRIS_PORT", "7777")
    monkeypatch.setenv("SCUFRIS_FULL_THINKING", "yes")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1, 2, 3")

    cfg = load_config(explicit_path=cfg_path, use_dotenv=False)
    # Env wins for model + port.
    assert cfg.ollama.model == "qwen3:32b"
    assert cfg.server.port == 7777
    # TOML still wins where env is unset.
    assert cfg.ollama.base_url == "http://from-toml:11434"
    assert cfg.server.bind == "0.0.0.0"
    assert cfg.client.server_url == "http://from-toml:9000"
    # Bool env override flips false → true.
    assert cfg.client.full_thinking is True
    # Comma-separated env list parses to tuple of ints.
    assert cfg.telegram.allowed_user_ids == (1, 2, 3)


def test_env_only_works_without_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SCUFRIS_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "home-empty"))
    monkeypatch.setenv("OLLAMA_MODEL", "from-env")
    monkeypatch.setenv("SCUFRIS_TOKEN", "shh")

    cfg = load_config(use_dotenv=False)
    assert cfg.ollama.model == "from-env"
    assert cfg.server.token == "shh"


def test_env_invalid_int_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCUFRIS_PORT", "not-a-number")
    with pytest.raises(ValueError, match="expected an integer"):
        load_config(use_dotenv=False)


def test_env_invalid_bool_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_REASONING", "maybe")
    with pytest.raises(ValueError, match="expected a boolean"):
        load_config(use_dotenv=False)


# ----------------------------------------------------------------------
# resolve_user_id
# ----------------------------------------------------------------------


def _config_with_bindings(**bindings: str) -> Config:
    return Config(
        user=UserSection(
            username="alex",
            identity=UserIdentity(bindings=bindings),
        )
    )


def test_resolve_matches_telegram_binding_to_username_hash() -> None:
    cfg = _config_with_bindings(telegram="42", cli="alex")
    res = resolve_user_id("telegram", "42", cfg)
    assert res == ResolvedIdentity(user_id=user_id_for("alex"), username="alex")


def test_resolve_matches_cli_binding_to_same_user_id() -> None:
    cfg = _config_with_bindings(telegram="42", cli="alex")
    tg = resolve_user_id("telegram", "42", cfg)
    cli = resolve_user_id("cli", "alex", cfg)
    assert tg.user_id == cli.user_id == user_id_for("alex")


def test_resolve_unknown_numeric_surface_id_passes_through_as_int() -> None:
    cfg = Config()
    res = resolve_user_id("telegram", "8231376426", cfg)
    assert res == ResolvedIdentity(user_id=8231376426, username=None)


def test_resolve_unknown_text_surface_id_falls_back_to_hash() -> None:
    cfg = Config()
    res = resolve_user_id("cli", "alex", cfg)
    assert res == ResolvedIdentity(user_id=user_id_for("alex"), username=None)


def test_resolve_binding_mismatch_falls_back_to_int() -> None:
    cfg = _config_with_bindings(telegram="42")
    res = resolve_user_id("telegram", "999", cfg)
    assert res == ResolvedIdentity(user_id=999, username=None)


def test_resolve_username_required_for_match() -> None:
    cfg = Config(
        user=UserSection(
            username=None, identity=UserIdentity(bindings={"telegram": "42"})
        )
    )
    res = resolve_user_id("telegram", "42", cfg)
    assert res == ResolvedIdentity(user_id=42, username=None)

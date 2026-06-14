"""Tests for ``scufris_server.config`` (step 2 of #9)."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from scufris_server.config import (
    Settings,
    _is_loopback_url,
    get_settings,
    warn_unsafe_settings,
)

_ENV_VARS = (
    "SCUFRIS_BIND",
    "SCUFRIS_PORT",
    "OPENCODE_URL",
    "OPENCODE_SERVER_PASSWORD",
    "SCUFRIS_STATE_DIR",
    "SCUFRIS_USER_ID",
    "SCUFRIS_CONFIG",
    "XDG_STATE_HOME",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip all settings-related env vars and clear the singleton cache.

    Without this, the host's env (or a previous test's leftovers) would
    leak into Settings(); and lru_cache would hand out a stale instance.
    """
    for k in _ENV_VARS:
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_basic_fields() -> None:
    s = Settings()
    assert s.bind == "127.0.0.1"
    assert s.port == 7080
    assert s.opencode_url == "http://127.0.0.1:4096"
    assert s.opencode_password is None
    # Identity (#12) — no override, no explicit config path by default.
    assert s.user_id is None
    assert s.config_path is None


def test_default_state_dir_falls_back_to_home() -> None:
    s = Settings()
    assert s.state_dir == Path.home() / ".local" / "state" / "scufris"


def test_default_state_dir_honors_xdg_state_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    s = Settings()
    assert s.state_dir == tmp_path / "scufris"


# ---------------------------------------------------------------------------
# Env-var overrides
# ---------------------------------------------------------------------------


def test_env_override_all_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SCUFRIS_BIND", "0.0.0.0")
    monkeypatch.setenv("SCUFRIS_PORT", "9090")
    monkeypatch.setenv("OPENCODE_URL", "http://opencode.internal:5000")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "hunter2")
    monkeypatch.setenv("SCUFRIS_STATE_DIR", str(tmp_path / "custom"))
    monkeypatch.setenv("SCUFRIS_USER_ID", "42")
    monkeypatch.setenv("SCUFRIS_CONFIG", str(tmp_path / "config.toml"))

    s = Settings()
    assert s.bind == "0.0.0.0"
    assert s.port == 9090
    assert s.opencode_url == "http://opencode.internal:5000"
    assert s.opencode_password == "hunter2"
    assert s.state_dir == tmp_path / "custom"
    assert s.user_id == 42
    assert s.config_path == tmp_path / "config.toml"


def test_port_must_be_in_tcp_range_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCUFRIS_PORT", "0")
    with pytest.raises(ValueError):
        Settings()


def test_port_must_be_in_tcp_range_high(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCUFRIS_PORT", "70000")
    with pytest.raises(ValueError):
        Settings()


def test_port_must_be_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCUFRIS_PORT", "not-a-number")
    with pytest.raises(ValueError):
        Settings()


# ---------------------------------------------------------------------------
# Identity-related fields (#12)
# ---------------------------------------------------------------------------


def test_user_id_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """``users.id`` is AUTOINCREMENT starting at 1; 0 / negatives can't bind."""
    monkeypatch.setenv("SCUFRIS_USER_ID", "0")
    with pytest.raises(ValueError):
        Settings()


def test_user_id_rejects_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCUFRIS_USER_ID", "-3")
    with pytest.raises(ValueError):
        Settings()


def test_user_id_rejects_non_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCUFRIS_USER_ID", "alex")
    with pytest.raises(ValueError):
        Settings()


def test_config_path_accepts_arbitrary_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The path doesn't have to exist at Settings-construction time."""
    target = tmp_path / "does" / "not" / "exist.toml"
    monkeypatch.setenv("SCUFRIS_CONFIG", str(target))
    s = Settings()
    assert s.config_path == target


# ---------------------------------------------------------------------------
# Singleton / cache semantics
# ---------------------------------------------------------------------------


def test_get_settings_returns_same_instance() -> None:
    a = get_settings()
    b = get_settings()
    assert a is b


def test_get_settings_cache_clear_re_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCUFRIS_BIND", "1.2.3.4")
    first = get_settings()
    assert first.bind == "1.2.3.4"

    # Mutating env without clearing the cache must NOT change the result.
    monkeypatch.setenv("SCUFRIS_BIND", "5.6.7.8")
    assert get_settings() is first
    assert get_settings().bind == "1.2.3.4"

    # cache_clear forces a fresh instantiation against current env.
    get_settings.cache_clear()
    fresh = get_settings()
    assert fresh is not first
    assert fresh.bind == "5.6.7.8"


# ---------------------------------------------------------------------------
# Loopback detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:4096",
        "http://127.0.0.1",
        "http://localhost:4096",
        "https://localhost",
        "http://[::1]:4096",
    ],
)
def test_is_loopback_url_recognises_loopback(url: str) -> None:
    assert _is_loopback_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:4096",
        "http://192.168.1.10:4096",
        "http://10.0.0.5",
        "https://opencode.internal:5000",
    ],
)
def test_is_loopback_url_rejects_remote(url: str) -> None:
    assert not _is_loopback_url(url)


# ---------------------------------------------------------------------------
# Unsafe-config warning
# ---------------------------------------------------------------------------


def test_warn_unsafe_silent_for_loopback_without_password() -> None:
    s = Settings(
        opencode_url="http://127.0.0.1:4096",
        opencode_password=None,
    )
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        warn_unsafe_settings(s)
    assert captured == []


def test_warn_unsafe_silent_for_remote_with_password() -> None:
    s = Settings(
        opencode_url="http://opencode.internal:5000",
        opencode_password="hunter2",
    )
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        warn_unsafe_settings(s)
    assert captured == []


def test_warn_unsafe_fires_for_remote_without_password() -> None:
    s = Settings(
        opencode_url="http://opencode.internal:5000",
        opencode_password=None,
    )
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        warn_unsafe_settings(s)
    assert len(captured) == 1
    assert issubclass(captured[0].category, UserWarning)
    msg = str(captured[0].message)
    assert "OPENCODE_SERVER_PASSWORD" in msg
    assert "opencode.internal" in msg

"""Tests for HTTP-touching tools (mocked at the SDK/transport boundary).

Covers `weather_tool` and `web_search_tool`. All network access is
monkeypatched — tests must not perform real I/O.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List

import pytest
import requests

from utils.tools.weather_tool import weather_tool
from utils.tools.web_search import web_search_tool

# `utils/tools/__init__.py` does `from .weather_tool import weather_tool`,
# which rebinds the parent package's `weather_tool` attribute to the
# StructuredTool. Attribute-style imports like
# `import utils.tools.weather_tool as weather_mod` therefore yield the
# tool, not the module. Reach into sys.modules to get the real modules.
weather_mod = sys.modules["utils.tools.weather_tool"]
search_mod = sys.modules["utils.tools.web_search"]


# ---------------------------------------------------------------------------
# weather_tool
# ---------------------------------------------------------------------------


def _wttr_payload(num_days: int = 3) -> Dict[str, Any]:
    """Build a minimal but realistic wttr.in j1 payload."""
    weather: List[Dict[str, Any]] = []
    for i in range(num_days):
        hourly = [{"weatherDesc": [{"value": f"Slot {h} day {i}"}]} for h in range(8)]
        weather.append(
            {
                "date": f"2026-05-{10 + i:02d}",
                "mintempC": str(10 + i),
                "maxtempC": str(20 + i),
                "hourly": hourly,
            }
        )
    return {
        "current_condition": [
            {
                "temp_C": "15",
                "FeelsLikeC": "14",
                "weatherDesc": [{"value": "Partly cloudy"}],
                "humidity": "60",
                "windspeedKmph": "10",
                "winddir16Point": "NW",
                "precipMM": "0.0",
                "visibility": "10",
            }
        ],
        "nearest_area": [
            {
                "areaName": [{"value": "Ploiesti"}],
                "country": [{"value": "Romania"}],
            }
        ],
        "weather": weather,
    }


class _FakeResponse:
    def __init__(self, payload: Dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> Dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"status {self.status_code}")


@pytest.fixture
def wttr_recorder(monkeypatch) -> Dict[str, Any]:
    """Patch `requests.get` and record the URL/timeout used."""
    calls: Dict[str, Any] = {}

    def fake_get(url, timeout=None, **kwargs):
        calls["url"] = url
        calls["timeout"] = timeout
        return _FakeResponse(_wttr_payload())

    monkeypatch.setattr(weather_mod.requests, "get", fake_get)
    return calls


def test_weather_returns_current_block_only_when_forecast_days_zero(wttr_recorder):
    out = weather_tool.invoke({"location": "Ploiesti", "forecast_days": 0})
    assert "Current weather in Ploiesti, Romania" in out
    assert "Forecast:" not in out


def test_weather_includes_two_dates_when_forecast_days_two(wttr_recorder):
    out = weather_tool.invoke({"location": "Ploiesti", "forecast_days": 2})
    assert "Forecast:" in out
    assert "2026-05-10" in out
    assert "2026-05-11" in out
    assert "2026-05-12" not in out


def test_weather_clamps_forecast_days_to_three(wttr_recorder):
    out = weather_tool.invoke({"location": "Ploiesti", "forecast_days": 99})
    # All 3 fixture dates appear
    for d in ("2026-05-10", "2026-05-11", "2026-05-12"):
        assert d in out


def test_weather_treats_negative_forecast_days_as_zero(wttr_recorder):
    out = weather_tool.invoke({"location": "Ploiesti", "forecast_days": -1})
    assert "Forecast:" not in out


def test_weather_uses_j1_format_and_ten_second_timeout(wttr_recorder):
    weather_tool.invoke({"location": "Ploiesti", "forecast_days": 0})
    assert "format=j1" in wttr_recorder["url"]
    assert wttr_recorder["timeout"] == 10


def test_weather_handles_timeout(monkeypatch):
    def boom(url, timeout=None, **kwargs):
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(weather_mod.requests, "get", boom)
    out = weather_tool.invoke({"location": "Mars", "forecast_days": 0})
    assert "Weather request timed out" in out


def test_weather_handles_request_exception(monkeypatch):
    def boom(url, timeout=None, **kwargs):
        raise requests.exceptions.ConnectionError("dns")

    monkeypatch.setattr(weather_mod.requests, "get", boom)
    out = weather_tool.invoke({"location": "Mars", "forecast_days": 0})
    assert "Failed to fetch weather" in out


def test_weather_handles_unparseable_payload(monkeypatch):
    monkeypatch.setattr(
        weather_mod.requests,
        "get",
        lambda url, timeout=None, **kw: _FakeResponse({}),
    )
    out = weather_tool.invoke({"location": "Mars", "forecast_days": 0})
    # Empty current_condition still renders ("N/A" placeholders); but
    # missing nearest_area falls back to location verbatim. Forecast
    # block omitted because `weather` key absent.
    assert "Mars" in out
    assert "Forecast:" not in out


# ---------------------------------------------------------------------------
# web_search_tool
# ---------------------------------------------------------------------------


class _FakeDDGS:
    """Stand-in for the `DDGS` context manager."""

    results: List[Dict[str, str]] = []
    raise_on_text: Exception | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def text(self, query, max_results=5):
        if self.raise_on_text:
            raise self.raise_on_text
        return list(self.results)


@pytest.fixture
def fake_ddgs(monkeypatch):
    monkeypatch.setattr(search_mod, "DDGS", _FakeDDGS)
    # Reset class-level state between tests
    _FakeDDGS.results = []
    _FakeDDGS.raise_on_text = None
    return _FakeDDGS


def test_web_search_formats_numbered_results_with_references(fake_ddgs):
    fake_ddgs.results = [
        {"title": "First", "body": "Body one", "href": "https://a"},
        {"title": "Second", "body": "Body two", "href": "https://b"},
    ]
    out = web_search_tool.invoke({"query": "anything"})
    assert "1. First" in out
    assert "2. Second" in out
    assert "📚 References:" in out
    assert "[1] https://a" in out
    assert "[2] https://b" in out


def test_web_search_returns_no_results_message_when_empty(fake_ddgs):
    fake_ddgs.results = []
    out = web_search_tool.invoke({"query": "nothing"})
    assert out == "No results found for the query."


def test_web_search_returns_failure_message_on_exception(fake_ddgs):
    fake_ddgs.raise_on_text = RuntimeError("upstream down")
    out = web_search_tool.invoke({"query": "anything"})
    assert out.startswith("Search failed:")
    assert "upstream down" in out

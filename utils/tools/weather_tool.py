"""Weather tool for the agent using wttr.in API.

Modernised to the `@tool` decorator (was a legacy single-input
`Tool(...)`), which broke the moment the LLM tried to pass a
forecast horizon — see ``tasks/20260509-165118``.
"""

import logging
from typing import Any, Dict, List

import requests

from ._decorator import tool

logger = logging.getLogger("scufris-bot.tools.weather")

WTTR_URL = "https://wttr.in/{location}?format=j1"
MAX_FORECAST_DAYS = 3  # wttr.in returns at most 3 days


def _format_current(current: Dict[str, Any], location_str: str) -> str:
    temp_c = current.get("temp_C", "N/A")
    feels_like_c = current.get("FeelsLikeC", "N/A")
    weather_desc = current.get("weatherDesc", [{}])[0].get("value", "N/A")
    humidity = current.get("humidity", "N/A")
    wind_speed = current.get("windspeedKmph", "N/A")
    wind_dir = current.get("winddir16Point", "N/A")
    precipitation = current.get("precipMM", "N/A")
    visibility = current.get("visibility", "N/A")

    return (
        f"Current weather in {location_str}:\n\n"
        f"Condition: {weather_desc}\n"
        f"Temperature: {temp_c}°C (feels like {feels_like_c}°C)\n"
        f"Humidity: {humidity}%\n"
        f"Wind: {wind_speed} km/h {wind_dir}\n"
        f"Precipitation: {precipitation} mm\n"
        f"Visibility: {visibility} km"
    )


def _format_forecast(weather: List[Dict[str, Any]], days: int) -> str:
    """Compact one-line-per-day summary from the wttr `weather` array."""
    lines: List[str] = []
    for entry in weather[:days]:
        date = entry.get("date", "?")
        mn = entry.get("mintempC", "?")
        mx = entry.get("maxtempC", "?")
        # wttr "weather" entries have an `hourly` block; the noon-ish slot
        # (index 4 of 8 = 12:00) tends to give the most representative
        # description for the day. Fall back to first slot if missing.
        hourly = entry.get("hourly") or []
        slot = hourly[4] if len(hourly) > 4 else (hourly[0] if hourly else {})
        desc = (slot.get("weatherDesc") or [{}])[0].get("value", "—")
        lines.append(f"  {date}: {mn}–{mx}°C, {desc}")
    return "Forecast:\n" + "\n".join(lines)


@tool("weather")
def weather_tool(location: str, forecast_days: int = 0) -> str:
    """Get current weather and an optional short-range forecast.

    Args:
        location: City or "city, country" (e.g. "Ploiesti", "Tokyo, Japan").
        forecast_days: 0 = current conditions only (default).
                       1–3 = also include a daily forecast for that many
                       days. Values >3 are clamped to 3; negatives to 0.

    Returns:
        Formatted weather information. With `forecast_days >= 1` the
        response includes both current conditions and a per-day forecast
        block underneath.
    """
    # Defensive clamp — don't trust the LLM to respect "1–3".
    forecast_days = max(0, min(int(forecast_days or 0), MAX_FORECAST_DAYS))

    try:
        url = WTTR_URL.format(location=location.strip())
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        current = data.get("current_condition", [{}])[0]
        nearest_area = data.get("nearest_area", [{}])[0]
        area_name = nearest_area.get("areaName", [{}])[0].get("value", location)
        country = nearest_area.get("country", [{}])[0].get("value", "")
        location_str = f"{area_name}, {country}" if country else area_name

        result = _format_current(current, location_str)
        if forecast_days > 0:
            forecast = data.get("weather") or []
            if forecast:
                result += "\n\n" + _format_forecast(forecast, forecast_days)
        return result

    except requests.exceptions.Timeout:
        logger.error(f"Weather request timeout for location: {location}")
        return f"Weather request timed out for '{location}'. Please try again."

    except requests.exceptions.RequestException as e:
        logger.error(f"Weather request error for location {location}: {e}")
        return f"Failed to fetch weather for '{location}': {str(e)}"

    except (KeyError, IndexError, ValueError) as e:
        logger.error(f"Weather data parsing error for location {location}: {e}")
        return (
            f"Failed to parse weather data for '{location}'. "
            "The location may not be found."
        )

    except Exception as e:
        logger.error(f"Unexpected weather error for location {location}: {e}")
        return f"An unexpected error occurred while fetching weather: {str(e)}"

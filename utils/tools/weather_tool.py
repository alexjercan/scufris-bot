"""Weather tool for the agent using wttr.in API."""

import logging

import requests
from langchain_core.tools import Tool

logger = logging.getLogger("scufris-bot.tools.weather")


def get_weather(location: str) -> str:
    """Get current weather information for a location using wttr.in.

    Args:
        location: The location name (e.g., 'Paris', 'New York', 'London')

    Returns:
        Formatted weather information with current conditions
    """
    try:
        # Construct wttr.in API URL with format parameter for plain text output
        # Using format=j1 for JSON format which is easier to parse
        url = f"https://wttr.in/{location.strip()}?format=j1"

        # Make the request with a timeout
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        # Parse JSON response
        data = response.json()

        # Extract current conditions
        current = data.get("current_condition", [{}])[0]
        nearest_area = data.get("nearest_area", [{}])[0]

        # Get location details
        area_name = nearest_area.get("areaName", [{}])[0].get("value", location)
        country = nearest_area.get("country", [{}])[0].get("value", "")

        # Get current weather data
        temp_c = current.get("temp_C", "N/A")
        feels_like_c = current.get("FeelsLikeC", "N/A")
        weather_desc = current.get("weatherDesc", [{}])[0].get("value", "N/A")
        humidity = current.get("humidity", "N/A")
        wind_speed = current.get("windspeedKmph", "N/A")
        wind_dir = current.get("winddir16Point", "N/A")
        precipitation = current.get("precipMM", "N/A")
        visibility = current.get("visibility", "N/A")

        # Format the response
        location_str = f"{area_name}, {country}" if country else area_name

        result = f"Current weather in {location_str}:\n\n"
        result += f"Condition: {weather_desc}\n"
        result += f"Temperature: {temp_c}°C (feels like {feels_like_c}°C)\n"
        result += f"Humidity: {humidity}%\n"
        result += f"Wind: {wind_speed} km/h {wind_dir}\n"
        result += f"Precipitation: {precipitation} mm\n"
        result += f"Visibility: {visibility} km"

        return result

    except requests.exceptions.Timeout:
        logger.error(f"Weather request timeout for location: {location}")
        return f"Weather request timed out for '{location}'. Please try again."

    except requests.exceptions.RequestException as e:
        logger.error(f"Weather request error for location {location}: {e}")
        return f"Failed to fetch weather for '{location}': {str(e)}"

    except (KeyError, IndexError, ValueError) as e:
        logger.error(f"Weather data parsing error for location {location}: {e}")
        return f"Failed to parse weather data for '{location}'. The location may not be found."

    except Exception as e:
        logger.error(f"Unexpected weather error for location {location}: {e}")
        return f"An unexpected error occurred while fetching weather: {str(e)}"


# Create the weather tool
weather_tool = Tool(
    name="weather",
    description=(
        "Get current weather information for any location worldwide. "
        "Use this when the user asks about weather, temperature, or current conditions. "
        "Input should be a location name (city, country, or coordinates). "
        "Examples: 'Paris', 'New York', 'Tokyo, Japan', 'London, UK'"
    ),
    func=get_weather,
)

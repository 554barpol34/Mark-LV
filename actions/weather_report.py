"""
actions/weather_report.py — real weather numbers, not a browser tab.

This used to open a Google search for "weather in <city>" and return "Showing
the weather". That reads fine out loud while the user looks at the browser, but
the tool itself never knew the weather, so anything built on top of it had
nothing to work with: agent mode asked for Istanbul's weather to save in a file
and could only save search-result links.

Now it asks Open-Meteo (free, no API key, no account): the city is geocoded,
then the current conditions and the daily forecast come back as numbers. If
the service cannot be reached, it falls back to the old behaviour and says so.
"""

import webbrowser
from urllib.parse import quote_plus

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 8

# WMO weather interpretation codes, as Open-Meteo reports them.
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}


def _describe(code) -> str:
    try:
        return _WMO.get(int(code), f"weather code {code}")
    except (TypeError, ValueError):
        return "unknown conditions"


def _fetch(city: str) -> dict:
    """Geocode the city and fetch its forecast. Raises on any failure."""
    import requests

    geo = requests.get(_GEOCODE_URL, params={"name": city, "count": 1, "format": "json"},
                       timeout=_TIMEOUT)
    geo.raise_for_status()
    places = geo.json().get("results") or []
    if not places:
        raise LookupError(f"no place called '{city}' was found")
    place = places[0]

    fc = requests.get(_FORECAST_URL, timeout=_TIMEOUT, params={
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max",
        "timezone": "auto",
        "forecast_days": 7,
    })
    fc.raise_for_status()
    data = fc.json()
    data["_place"] = place
    return data


def _format(data: dict, day: str) -> str:
    place = data["_place"]
    name = ", ".join(p for p in (place.get("name"), place.get("country")) if p)
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    days = daily.get("time") or []

    def _day_line(i: int) -> str:
        rain = (daily.get("precipitation_probability_max") or [None] * len(days))[i]
        rain_txt = f", {rain}% chance of rain" if rain is not None else ""
        return (f"{days[i]}: {_describe(daily['weather_code'][i])}, "
                f"{daily['temperature_2m_min'][i]:.0f}–{daily['temperature_2m_max'][i]:.0f}°C"
                f"{rain_txt}")

    lines = [f"Weather for {name} (source: Open-Meteo)"]
    if day in ("today", "now", "") and cur:
        lines.append(
            f"Now: {_describe(cur.get('weather_code'))}, {cur.get('temperature_2m'):.0f}°C "
            f"(feels like {cur.get('apparent_temperature'):.0f}°C), humidity "
            f"{cur.get('relative_humidity_2m')}%, wind {cur.get('wind_speed_10m'):.0f} km/h")
    if days:
        if day == "week":
            lines += [_day_line(i) for i in range(len(days))]
        elif day == "tomorrow" and len(days) > 1:
            lines.append("Tomorrow " + _day_line(1))
        else:
            lines.append("Today " + _day_line(0))
    return "\n".join(lines)


def _open_in_browser(city: str, day: str, reason: str, player) -> str:
    query = f"weather in {city} {day}"
    try:
        webbrowser.open(f"https://www.google.com/search?q={quote_plus(query)}")
        msg = (f"I could not fetch the weather data ({reason}), so I opened the "
               f"weather for {city} in the browser instead. I do not have the numbers.")
    except Exception as e:
        msg = f"I could not get the weather for {city}: {reason}; the browser also failed: {e}"
    _log(msg, player)
    return msg


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    city = parameters.get("city")
    day = str(parameters.get("day") or parameters.get("time") or "today").strip().lower()
    if day not in ("today", "now", "tomorrow", "week"):
        day = "today"

    if not city or not isinstance(city, str) or not city.strip():
        msg = "Sir, the city is missing for the weather report."
        _log(msg, player)
        return msg
    city = city.strip()

    try:
        report = _format(_fetch(city), day)
    except LookupError:
        msg = f"I could not find a place called '{city}'."
        _log(msg, player)
        return msg
    except Exception as e:
        return _open_in_browser(city, day, str(e)[:120], player)

    _log(report.splitlines()[1] if "\n" in report else report, player)
    if session_memory:
        try:
            session_memory.set_last_search(query=f"weather in {city} {day}", response=report)
        except Exception:
            pass
    return report


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "weather_report",
    "description": (
        "Returns the real weather for a city: current temperature, feels-like, "
        "conditions, humidity and wind, plus today's, tomorrow's or the 7-day "
        "forecast with min/max temperatures and chance of rain. Use for ANY "
        "weather question instead of web_search."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "city": {
                "type": "STRING",
                "description": "City name"
            },
            "day": {
                "type": "STRING",
                "description": "today (default, includes current conditions) | tomorrow | week",
                "enum": ["today", "tomorrow", "week"],
            },
        },
        "required": [
            "city"
        ]
    },
    "handler": weather_action,
}

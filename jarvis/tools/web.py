"""Quick web lookups that need no browser window: search, fetch a page's text, weather."""
from __future__ import annotations

import re

from jarvis.tools import tool

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


@tool("web_search", """Search the web and get titles, links and short snippets. Snippets are NOT
the answer: open or fetch the pages to get real data.""",
      {"query": {"type": "string"}, "max_results": {"type": "integer"}}, ["query"],
      label=lambda a: f"Aranıyor: {a.get('query')}")
def web_search(query: str, max_results: int = 8):
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    rows = list(DDGS().text(query, max_results=max(1, min(int(max_results), 15))))
    if not rows:
        return "No results."
    return "\n\n".join(f"{i}. {r.get('title')}\n{r.get('href')}\n{r.get('body')}"
                       for i, r in enumerate(rows, 1))


@tool("fetch_page", """Download a web page and return its readable text (fast, no window). Use it
for articles and simple pages; use the browser for sites that need clicking, logging in or
JavaScript.""",
      {"url": {"type": "string"}}, ["url"], label=lambda a: f"Okunuyor: {a.get('url', '')[:60]}")
def fetch_page(url: str):
    import requests
    from bs4 import BeautifulSoup
    if not url.startswith("http"):
        url = "https://" + url
    r = requests.get(url, headers={"User-Agent": _UA, "Accept-Language": "tr,en;q=0.8"}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        t.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\n\s*\n+", "\n\n", main.get_text("\n", strip=True))
    if len(text) < 200:
        text += "\n(Very little text: this page probably needs JavaScript; open it with browser_open.)"
    return f"{title}\n{r.url}\n\n{text}"


_WMO = {0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
        65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow", 80: "showers",
        81: "showers", 82: "heavy showers", 95: "thunderstorm", 96: "thunderstorm, hail",
        99: "thunderstorm, hail"}


@tool("weather", "Real weather and 7-day forecast for a city (Open-Meteo).",
      {"city": {"type": "string"}}, ["city"], label=lambda a: f"Hava durumu: {a.get('city')}")
def weather(city: str):
    import requests
    g = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                     params={"name": city, "count": 1}, timeout=10).json().get("results")
    if not g:
        return f"No place called {city!r}."
    p = g[0]
    d = requests.get("https://api.open-meteo.com/v1/forecast", timeout=10, params={
        "latitude": p["latitude"], "longitude": p["longitude"], "timezone": "auto",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    }).json()
    c, dl = d["current"], d["daily"]
    lines = [f"{p['name']}, {p.get('country', '')} now: {_WMO.get(c['weather_code'], c['weather_code'])}, "
             f"{c['temperature_2m']}°C (feels {c['apparent_temperature']}°C), humidity "
             f"{c['relative_humidity_2m']}%, wind {c['wind_speed_10m']} km/h"]
    for i, day in enumerate(dl["time"]):
        lines.append(f"{day}: {_WMO.get(dl['weather_code'][i], '?')}, {dl['temperature_2m_min'][i]}–"
                     f"{dl['temperature_2m_max'][i]}°C, rain {dl['precipitation_probability_max'][i]}%")
    return "\n".join(lines)

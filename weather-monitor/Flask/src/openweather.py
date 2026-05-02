import requests

try:
    from src.clients import API_KEY
except ImportError:
    from clients import API_KEY

OPENWEATHER_BASE = "https://api.openweathermap.org"
GEOCODE_URL = f"{OPENWEATHER_BASE}/geo/1.0/direct"
WEATHER_URL = f"{OPENWEATHER_BASE}/data/2.5/weather"
FORECAST_URL = f"{OPENWEATHER_BASE}/data/2.5/forecast"


def geocode(city):
    r = requests.get(
        GEOCODE_URL,
        params={"q": city, "limit": 1, "appid": API_KEY},
        timeout=10,
    )
    r.raise_for_status()
    results = r.json()
    if not results:
        return None
    return results[0]["lat"], results[0]["lon"]


def fetch_current(city):
    coords = geocode(city)
    if coords is None:
        return None
    lat, lon = coords
    r = requests.get(
        WEATHER_URL,
        params={"lat": lat, "lon": lon, "appid": API_KEY, "units": "metric"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def fetch_forecast(city):
    coords = geocode(city)
    if coords is None:
        return None
    lat, lon = coords
    r = requests.get(
        FORECAST_URL,
        params={"lat": lat, "lon": lon, "appid": API_KEY, "units": "metric"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()

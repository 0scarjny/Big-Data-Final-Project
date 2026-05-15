# Forecast data fetcher + parser for the M5 Stack.
#
# Talks to the Flask backend's /get_forecast endpoint (which proxies
# OpenWeather's 5-day / 3-hour forecast). Produces two summarised views:
#   today_buckets() — up to 6 of today's 3-hour slots from now forward
#   week_days()     — 5 daily summaries (min/max temp + representative icon)
#
# All HTTP work runs in the caller's thread (typically a worker spawned from
# main.py's forecast_task) so the asyncio loop is never blocked.

import time
import requests2

from config import SHARED_SECRET, FORECAST_URL

DEBUG = True  # flip to False once everything works

HTTP_TIMEOUT_S = 25  # Cloud Run cold starts can take 15-20 s

DEFAULT_CITY = "Lausanne"

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

def _log(*args):
    if DEBUG:
        print("[forecast]", *args)

def fetch(city=None):
    """Fetch the raw forecast JSON. Returns dict on success, None on failure."""
    city = city or DEFAULT_CITY
    payload = {"passwd": SHARED_SECRET, "city": city}
    try:
        resp = requests2.post(FORECAST_URL, json=payload, timeout=HTTP_TIMEOUT_S)
        if resp.status_code != 200:
            _log("HTTP", resp.status_code)
            resp.close()
            return None
        data = resp.json()
        resp.close()
        if data.get("status") != "success":
            _log("backend error:", data.get("error"))
            return None
        return data.get("data") or None
    except Exception as e:
        _log("fetch error:", e)
        return None


def _local_struct(ts_utc, tz_offset_s):
    """Return a struct_time whose fields read as the city's wall clock.

    Uses gmtime (not localtime) so the result is independent of the host
    system's timezone. gmtime exists in both CPython and MicroPython and
    performs a pure UTC unpack; adding the city's offset to the UTC
    timestamp shifts the resulting fields to the city's local time.
    """
    return time.gmtime(ts_utc + tz_offset_s)


def today_buckets(data, max_slots=6):
    """List of the next max_slots upcoming 3-hour forecast slots.

    Takes the next N entries from the forecast list that have not yet passed,
    regardless of day boundary.  This means late in the day the view
    seamlessly shows tomorrow's slots without any special-casing, and any
    API update is fully reflected on the next refresh.  Each entry:
    {hour, icon, temp, description}.
    """
    if not data:
        return []
    tz = (data.get("city") or {}).get("timezone", 0)
    now = time.time()
    out = []
    for item in data.get("list", []):
        dt = item.get("dt", 0)
        if dt < now - 600:  # skip already-past slots (10 min grace)
            continue
        local = _local_struct(dt, tz)
        weather = (item.get("weather") or [{}])[0]
        main = item.get("main") or {}
        out.append({
            "hour": local[3],
            "icon": weather.get("icon") or "01d",
            "temp": main.get("temp"),
            "description": weather.get("description") or "",
        })
        if len(out) >= max_slots:
            break
    return out


def week_days(data, max_days=5):
    """List of dicts: {day_name, date, temp_min, temp_max, icon, description}
    aggregating each day in the forecast. Picks the mid-day bucket as the
    representative icon and the day's min/max across all buckets."""
    if not data:
        return []
    tz = (data.get("city") or {}).get("timezone", 0)
    by_day = {}
    order = []

    for item in data.get("list", []):
        dt = item.get("dt", 0)
        local = _local_struct(dt, tz)
        # Use date string (yyyy-mm-dd) as the dict key so we don't depend on
        # day-of-year crossing year boundaries.
        key = "{:04d}-{:02d}-{:02d}".format(local[0], local[1], local[2])
        weather = (item.get("weather") or [{}])[0]
        main = item.get("main") or {}
        temp = main.get("temp")
        if key not in by_day:
            by_day[key] = {
                "key": key,
                "weekday": local[6],
                "temp_min": temp,
                "temp_max": temp,
                "icon": weather.get("icon") or "01d",
                "description": weather.get("description") or "",
                "best_hour_diff": 24,
            }
            order.append(key)
        bucket = by_day[key]
        if temp is not None:
            if bucket["temp_min"] is None or temp < bucket["temp_min"]:
                bucket["temp_min"] = temp
            if bucket["temp_max"] is None or temp > bucket["temp_max"]:
                bucket["temp_max"] = temp
        # Prefer the bucket closest to noon for the day's representative icon.
        diff = abs(local[3] - 13)
        if diff < bucket["best_hour_diff"]:
            bucket["best_hour_diff"] = diff
            bucket["icon"] = weather.get("icon") or bucket["icon"]
            bucket["description"] = weather.get("description") or bucket["description"]

    out = []
    for key in order[:max_days]:
        b = by_day[key]
        out.append({
            "day_name": DAY_NAMES[b["weekday"] % 7],
            "date": key,
            "temp_min": b["temp_min"],
            "temp_max": b["temp_max"],
            "icon": b["icon"],
            "description": b["description"],
        })
    return out


# OpenWeather icon code -> codepoint in the Erik Flowers Weather Icons font.
# Cross-reference with weather-icons/css/weather-icons.css when adjusting.
# The font must be converted to LVGL .bin and placed at
# S:/flash/res/font/weather_icons_<size>.bin (see init() in ui.py).
_OWM_TO_CODEPOINT = {
    "01d": 0xF00D,  # wi-day-sunny
    "01n": 0xF02E,  # wi-night-clear
    "02d": 0xF002,  # wi-day-cloudy
    "02n": 0xF086,  # wi-night-alt-cloudy
    "03d": 0xF041,  # wi-cloud
    "03n": 0xF041,
    "04d": 0xF013,  # wi-cloudy
    "04n": 0xF013,
    "09d": 0xF009,  # wi-day-showers
    "09n": 0xF029,  # wi-night-alt-showers
    "10d": 0xF008,  # wi-day-rain
    "10n": 0xF028,  # wi-night-alt-rain
    "11d": 0xF010,  # wi-day-thunderstorm
    "11n": 0xF02D,  # wi-night-alt-thunderstorm
    "13d": 0xF00A,  # wi-day-snow
    "13n": 0xF02A,  # wi-night-alt-snow
    "50d": 0xF014,  # wi-fog
    "50n": 0xF014,
}


def icon_glyph(code):
    """Return the weather-icons glyph (single char) for an OpenWeather code."""
    cp = _OWM_TO_CODEPOINT.get(code or "01d", _OWM_TO_CODEPOINT["01d"])
    return chr(cp)

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
import json
import requests2

FORECAST_URL = 'https://flask-app-868833155300.europe-west6.run.app/get_forecast'
SHARED_SECRET = '03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4'
HTTP_TIMEOUT_S = 15

DEFAULT_CITY = "Lausanne"

CACHE_FILE = 'forecast_cache.json'

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def load_cache():
    """Return the last successfully fetched forecast from flash, or None."""
    try:
        with open(CACHE_FILE, 'r') as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return None


def _save_cache(data):
    try:
        with open(CACHE_FILE, 'w') as f:
            f.write(json.dumps(data))
    except Exception as e:
        print("[forecast] cache write error:", e)


def fetch(city=None):
    """Fetch the raw forecast JSON. Returns dict on success, None on failure.
    On success, writes the result to CACHE_FILE so the next boot can show
    stale data immediately while the fresh fetch runs in the background."""
    city = city or DEFAULT_CITY
    payload = {"passwd": SHARED_SECRET, "city": city}
    try:
        resp = requests2.post(FORECAST_URL, json=payload, timeout=HTTP_TIMEOUT_S)
        if resp.status_code != 200:
            print("[forecast] HTTP", resp.status_code)
            resp.close()
            return None
        data = resp.json()
        resp.close()
        if data.get("status") != "success":
            print("[forecast] backend error:", data.get("error"))
            return None
        result = data.get("data") or None
        if result is not None:
            _save_cache(result)
        return result
    except Exception as e:
        print("[forecast] fetch error:", e)
        return None


def _local_struct(ts_utc, tz_offset_s):
    """Convert a UTC unix timestamp to a localtime struct in the city's tz.

    MicroPython's time.localtime takes seconds; the simplest portable trick is
    to add the city offset to the UTC timestamp and call localtime — that
    yields fields that read as the city's wall clock.
    """
    return time.localtime(ts_utc + tz_offset_s)


def today_buckets(data, max_slots=6):
    """List of dicts: {hour, icon, temp, description} for today's remaining
    3-hour buckets, capped at max_slots."""
    if not data:
        return []
    tz = (data.get("city") or {}).get("timezone", 0)
    now = time.time()
    out = []
    today_yday = _local_struct(int(now), tz)[7]  # day-of-year for "today"

    for item in data.get("list", []):
        dt = item.get("dt", 0)
        if dt < now - 600:  # skip already-past slots (with 10 min grace)
            continue
        local = _local_struct(dt, tz)
        if local[7] != today_yday:
            break  # next day; stop
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

    # Edge case: if "today" is nearly over there might be 0–1 slots left.
    # Fall back to the next available slots so the view never goes empty.
    if len(out) < 2:
        out = []
        for item in data.get("list", []):
            dt = item.get("dt", 0)
            if dt < now - 600:
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


def icon_path(code):
    """Filesystem path on the device for a given OpenWeather icon code."""
    if not code:
        code = "01d"
    return "S:/flash/res/img/weather/{}.png".format(code)

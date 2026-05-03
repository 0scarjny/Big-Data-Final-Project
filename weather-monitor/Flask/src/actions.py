"""Action handlers — return structured fact dicts (not English strings).

Each function returns a dict the LLM-powered formatter can rephrase into
a natural sentence in any language. Keeping data raw here means we never
generate English just to translate it back; the formatter picks the right
units, phrasing and pluralisation per locale.

Status codes:
  ok        — the action ran and produced data
  no_data   — query succeeded but returned nothing for that period
  bad_input — caller supplied an unknown metric / future day / etc.
  error     — backend failure (BigQuery, OpenWeather, ...)
"""

from datetime import date, datetime, timedelta, timezone

from google.cloud import bigquery

try:
    from src.clients import client, WEATHER_TABLE_PATH
    from src import openweather
    from src.logger import get_logger
except ImportError:
    from clients import client, WEATHER_TABLE_PATH
    import openweather
    from logger import get_logger

log = get_logger("voice_assistant.actions")


# Metric metadata. label is purely for human reference; the formatter LLM uses
# it as a hint but is free to translate. unit is what we'd say out loud.
METRICS = {
    "indoor_temp":     {"label": "indoor temperature", "unit": "°C", "decimals": 1},
    "indoor_humidity": {"label": "indoor humidity",    "unit": "%",  "decimals": 0},
    "indoor_co2":      {"label": "indoor CO2",         "unit": "ppm", "decimals": 0},
}

def _today():
    return date.today()


def _round(value, decimals):
    if value is None:
        return None
    return round(float(value), decimals)


def _metric_meta(metric):
    return METRICS.get(metric)


def historical_indoor(metric, day_offset):
    meta = _metric_meta(metric)
    if meta is None:
        return {"intent": "historical_indoor", "status": "bad_input", "reason": "unknown_metric", "metric": metric}
    if day_offset > 0:
        return {"intent": "historical_indoor", "status": "bad_input", "reason": "future_date", "day_offset": day_offset}

    target = _today() + timedelta(days=day_offset)
    sql = f"""
        SELECT
            AVG({metric}) AS avg_v,
            MAX({metric}) AS max_v,
            MIN({metric}) AS min_v,
            COUNT(*)      AS n
        FROM `{WEATHER_TABLE_PATH}`
        WHERE date = @target_date
    """
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("target_date", "STRING", target.isoformat())]
        ),
    )
    row = list(job.result())[0]
    base = {
        "intent": "historical_indoor",
        "metric": metric,
        "metric_label": meta["label"],
        "unit": meta["unit"],
        "day_offset": day_offset,
        "date": target.isoformat(),
    }
    if not row["n"]:
        return {**base, "status": "no_data"}
    return {
        **base,
        "status": "ok",
        "avg": _round(row["avg_v"], meta["decimals"]),
        "min": _round(row["min_v"], meta["decimals"]),
        "max": _round(row["max_v"], meta["decimals"]),
        "samples": int(row["n"]),
    }


def threshold_check(metric, threshold, comparator, day_offset):
    meta = _metric_meta(metric)
    if meta is None:
        return {"intent": "threshold_check", "status": "bad_input", "reason": "unknown_metric", "metric": metric}
    if comparator not in ("above", "below"):
        return {"intent": "threshold_check", "status": "bad_input", "reason": "bad_comparator", "comparator": comparator}
    if day_offset > 0:
        return {"intent": "threshold_check", "status": "bad_input", "reason": "future_date", "day_offset": day_offset}

    target = _today() + timedelta(days=day_offset)
    sql = f"""
        SELECT MAX({metric}) AS max_v, MIN({metric}) AS min_v, COUNT(*) AS n
        FROM `{WEATHER_TABLE_PATH}`
        WHERE date = @target_date
    """
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("target_date", "STRING", target.isoformat())]
        ),
    )
    row = list(job.result())[0]
    base = {
        "intent": "threshold_check",
        "metric": metric,
        "metric_label": meta["label"],
        "unit": meta["unit"],
        "threshold": threshold,
        "comparator": comparator,
        "day_offset": day_offset,
        "date": target.isoformat(),
    }
    if not row["n"]:
        return {**base, "status": "no_data"}

    if comparator == "above":
        crossed = row["max_v"] > threshold
        extreme = _round(row["max_v"], meta["decimals"])
    else:
        crossed = row["min_v"] < threshold
        extreme = _round(row["min_v"], meta["decimals"])

    return {
        **base,
        "status": "ok",
        "crossed": bool(crossed),
        "extreme": extreme,
        "extreme_kind": "max" if comparator == "above" else "min",
    }


def current_indoor(metric):
    meta = _metric_meta(metric)
    if meta is None:
        return {"intent": "current_indoor", "status": "bad_input", "reason": "unknown_metric", "metric": metric}

    sql = f"""
        SELECT {metric} AS v, date, time
        FROM `{WEATHER_TABLE_PATH}`
        ORDER BY date DESC, time DESC
        LIMIT 1
    """
    rows = list(client.query(sql).result())
    base = {
        "intent": "current_indoor",
        "metric": metric,
        "metric_label": meta["label"],
        "unit": meta["unit"],
    }
    if not rows:
        return {**base, "status": "no_data"}
    row = rows[0]
    return {
        **base,
        "status": "ok",
        "value": _round(row["v"], meta["decimals"]),
        "measured_at": f"{row['date']} {row['time']}",
    }


def forecast_weather(hours_ahead=24, city=None):
    """General weather forecast for the next N hours: temperature range,
    dominant condition, rain info, humidity. Replaces the old umbrella-only
    action so the formatter can answer 'what's the weather like tomorrow?'
    as easily as 'do I need an umbrella?'.

    `city` must be provided by the caller (the device sends its own location
    via X-Device-Location; the user can override per-question by mentioning
    a place name in their question)."""
    if not city:
        return {
            "intent": "forecast_weather",
            "status": "bad_input",
            "reason": "missing_city",
            "hours_ahead": hours_ahead,
        }
    base = {"intent": "forecast_weather", "city": city, "hours_ahead": hours_ahead}
    try:
        data = openweather.fetch_forecast(city)
    except Exception as e:
        log.error("OpenWeather forecast failed: %s", e)
        return {**base, "status": "error", "reason": "forecast_unavailable"}
    if data is None:
        return {**base, "status": "bad_input", "reason": "city_not_found"}

    # OpenWeather returns UTC timestamps + the city's offset in seconds. Use it
    # so 'tomorrow morning' lines up with the user's local clock.
    tz_offset_s = (data.get("city") or {}).get("timezone", 0)
    now_ts = datetime.now(tz=timezone.utc).timestamp()
    cutoff = now_ts + hours_ahead * 3600

    temps = []
    humidities = []
    condition_counts = {}
    rain_buckets = []
    buckets_in_window = 0

    for item in data.get("list", []):
        ts = item.get("dt", 0)
        if ts > cutoff:
            break
        buckets_in_window += 1
        main = item.get("main") or {}
        if "temp" in main:
            temps.append(main["temp"])
        if "humidity" in main:
            humidities.append(main["humidity"])
        weather = (item.get("weather") or [{}])[0]
        cond = weather.get("main", "Unknown")
        condition_counts[cond] = condition_counts.get(cond, 0) + 1
        if cond == "Rain" or item.get("rain"):
            local_dt = datetime.fromtimestamp(ts + tz_offset_s, tz=timezone.utc)
            rain_buckets.append({
                "when": local_dt.strftime("%A %H:%M"),
                "description": weather.get("description") or "rain",
            })

    if not temps:
        return {**base, "status": "no_data"}

    dominant = max(condition_counts.items(), key=lambda kv: kv[1])[0] if condition_counts else "Unknown"

    return {
        **base,
        "status": "ok",
        "temp_min": round(min(temps), 1),
        "temp_max": round(max(temps), 1),
        "temp_unit": "°C",
        "dominant_condition": dominant,
        "all_conditions": sorted(condition_counts.keys()),
        "humidity_avg": round(sum(humidities) / len(humidities)) if humidities else None,
        "humidity_unit": "%",
        "rain_expected": bool(rain_buckets),
        "first_rain": rain_buckets[0] if rain_buckets else None,
        "rain_buckets": len(rain_buckets),
        "total_buckets": buckets_in_window,
    }


def dispatch(intent):
    """Returns a structured facts dict. Never raises — errors become a dict
    with status='error' so the formatter can phrase it for the user."""
    action = (intent or {}).get("action", "unknown")
    log.info("Dispatching action: %s (full intent: %s)", action, intent)
    try:
        if action == "historical_indoor":
            facts = historical_indoor(intent["metric"], int(intent.get("day_offset", -1)))
        elif action == "threshold_check":
            facts = threshold_check(
                intent["metric"],
                float(intent["threshold"]),
                intent.get("comparator", "above"),
                int(intent.get("day_offset", -1)),
            )
        elif action == "current_indoor":
            facts = current_indoor(intent["metric"])
        elif action in ("forecast_weather", "forecast_umbrella"):
            # Accept the legacy intent name so an in-flight Gemini cache or
            # older prompt still routes correctly.
            facts = forecast_weather(int(intent.get("hours_ahead", 24)), intent.get("city"))
        else:
            facts = {"intent": "unknown", "status": "unknown_intent"}
        log.info("Action facts: %s", facts)
        return facts
    except KeyError as e:
        log.warning("Intent missing required field: %s — intent was: %s", e, intent)
        return {"intent": action, "status": "bad_input", "reason": "missing_field", "field": str(e)}
    except Exception as e:
        log.error("Dispatch error for action=%s: %s: %s", action, type(e).__name__, e, exc_info=True)
        return {"intent": action, "status": "error", "reason": "internal_error"}

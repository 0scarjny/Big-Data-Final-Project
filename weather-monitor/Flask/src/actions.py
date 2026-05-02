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


ALLOWED_METRICS = {
    "indoor_temp": ("temperature", "{:.1f} degrees Celsius"),
    "indoor_humidity": ("humidity", "{:.0f} percent"),
    "indoor_co2": ("CO2", "{:.0f} ppm"),
}

DEFAULT_CITY = "Lausanne"


def _today():
    return date.today()



def _format_offset(day_offset):
    if day_offset == -1:
        return "yesterday"
    if day_offset == 0:
        return "today"
    return f"{abs(day_offset)} days ago"


def _format_value(metric, value):
    return ALLOWED_METRICS[metric][1].format(value)


def _metric_label(metric):
    return ALLOWED_METRICS[metric][0]


def historical_indoor(metric, day_offset):
    if metric not in ALLOWED_METRICS:
        return "Sorry, I don't track that metric."
    if day_offset > 0:
        return "I can only look at past days, not the future."

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
    if not row["n"]:
        return f"I don't have {_metric_label(metric)} data for {_format_offset(day_offset)}."
    return (
        f"The average {_metric_label(metric)} {_format_offset(day_offset)} was "
        f"{_format_value(metric, row['avg_v'])}, ranging from "
        f"{_format_value(metric, row['min_v'])} to {_format_value(metric, row['max_v'])}."
    )


def threshold_check(metric, threshold, comparator, day_offset):
    if metric not in ALLOWED_METRICS:
        return "Sorry, I don't track that metric."
    if comparator not in ("above", "below"):
        return "I didn't understand the comparison."
    if day_offset > 0:
        return "I can only look at past days, not the future."

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
    if not row["n"]:
        return f"I don't have {_metric_label(metric)} data for {_format_offset(day_offset)}."

    if comparator == "above":
        crossed = row["max_v"] > threshold
        peak = row["max_v"]
    else:
        crossed = row["min_v"] < threshold
        peak = row["min_v"]

    label = _metric_label(metric)
    when = _format_offset(day_offset)
    if crossed:
        return f"Yes, {label} went {comparator} {threshold} {when}, reaching {_format_value(metric, peak)}."
    return f"No, {label} stayed {('below' if comparator == 'above' else 'above')} {threshold} {when}. The {('peak' if comparator == 'above' else 'low')} was {_format_value(metric, peak)}."


def current_indoor(metric):
    if metric not in ALLOWED_METRICS:
        return "Sorry, I don't track that metric."

    sql = f"""
        SELECT {metric} AS v, date, time
        FROM `{WEATHER_TABLE_PATH}`
        ORDER BY date DESC, time DESC
        LIMIT 1
    """
    rows = list(client.query(sql).result())
    if not rows:
        return "I don't have any readings yet."
    row = rows[0]
    return f"The latest {_metric_label(metric)} reading is {_format_value(metric, row['v'])}, taken at {row['time']} on {row['date']}."


def forecast_umbrella(hours_ahead=24, city=None):
    city = city or DEFAULT_CITY
    try:
        data = openweather.fetch_forecast(city)
    except Exception as e:
        return f"I couldn't reach the forecast service: {e}"
    if data is None:
        return f"I couldn't find the city {city}."

    now_ts = datetime.now(tz=timezone.utc).timestamp()
    cutoff = now_ts + hours_ahead * 3600
    rain_buckets = []
    for item in data.get("list", []):
        ts = item.get("dt", 0)
        if ts > cutoff:
            break
        weather = (item.get("weather") or [{}])[0]
        if weather.get("main") == "Rain" or item.get("rain"):
            local_dt = datetime.fromtimestamp(ts).strftime("%A %H:%M")
            rain_buckets.append(local_dt)

    if not rain_buckets:
        return f"No rain expected in the next {hours_ahead} hours in {city}. You can leave the umbrella at home."
    first = rain_buckets[0]
    return f"Yes, rain is expected starting {first} in {city}. Take an umbrella."


def dispatch(intent):
    action = (intent or {}).get("action", "unknown")
    log.info("Dispatching action: %s (full intent: %s)", action, intent)
    try:
        if action == "historical_indoor":
            result = historical_indoor(intent["metric"], int(intent.get("day_offset", -1)))
        elif action == "threshold_check":
            result = threshold_check(
                intent["metric"],
                float(intent["threshold"]),
                intent.get("comparator", "above"),
                int(intent.get("day_offset", -1)),
            )
        elif action == "current_indoor":
            result = current_indoor(intent["metric"])
        elif action == "forecast_umbrella":
            result = forecast_umbrella(int(intent.get("hours_ahead", 24)), intent.get("city"))
        else:
            return "Sorry, I didn't understand the question."
        log.info("Action reply: %r", result)
        return result
    except KeyError as e:
        log.warning("Intent missing required field: %s — intent was: %s", e, intent)
        return f"I'm missing some information to answer that: {e}"
    except Exception as e:
        log.error("Dispatch error for action=%s: %s: %s", action, type(e).__name__, e, exc_info=True)
        return "Something went wrong while looking that up."

"""Parameterised BigQuery SQL for the dashboard.

`date` and `time` are stored as STRINGs by the Flask ingestion path, so we
build a real TIMESTAMP at the SQL boundary and expose it as `ts`.
"""
from __future__ import annotations

from config import BQ_TABLE_PATH

_TS_EXPR = "PARSE_TIMESTAMP('%F %T', CONCAT(date, ' ', time))"

LATEST_READING = f"""
SELECT
    {_TS_EXPR} AS ts,
    date, time, location,
    indoor_temp, indoor_humidity, indoor_co2,
    outdoor_temp, outdoor_humidity, outdoor_weather
FROM {BQ_TABLE_PATH}
ORDER BY ts DESC
LIMIT 2
"""

RANGE_READINGS = f"""
SELECT
    {_TS_EXPR} AS ts,
    location,
    indoor_temp, indoor_humidity, indoor_co2,
    outdoor_temp, outdoor_humidity, outdoor_weather
FROM {BQ_TABLE_PATH}
WHERE date BETWEEN @start_date AND @end_date
ORDER BY ts ASC
LIMIT 50000
"""

HOURLY_AGGREGATES = f"""
WITH base AS (
    SELECT
        {_TS_EXPR} AS ts,
        indoor_temp, indoor_humidity, indoor_co2,
        outdoor_temp, outdoor_humidity
    FROM {BQ_TABLE_PATH}
    WHERE date BETWEEN @start_date AND @end_date
)
SELECT
    TIMESTAMP_TRUNC(ts, HOUR) AS ts,
    AVG(indoor_temp)     AS indoor_temp,
    AVG(indoor_humidity) AS indoor_humidity,
    AVG(indoor_co2)      AS indoor_co2,
    AVG(outdoor_temp)    AS outdoor_temp,
    AVG(outdoor_humidity) AS outdoor_humidity
FROM base
GROUP BY ts
ORDER BY ts ASC
"""

DAILY_SUMMARY = f"""
SELECT
    date,
    MIN(indoor_temp)      AS indoor_temp_min,
    AVG(indoor_temp)      AS indoor_temp_avg,
    MAX(indoor_temp)      AS indoor_temp_max,
    MIN(indoor_humidity)  AS indoor_humidity_min,
    AVG(indoor_humidity)  AS indoor_humidity_avg,
    MAX(indoor_humidity)  AS indoor_humidity_max,
    MIN(indoor_co2)       AS indoor_co2_min,
    AVG(indoor_co2)       AS indoor_co2_avg,
    MAX(indoor_co2)       AS indoor_co2_max,
    COUNT(*)              AS sample_count
FROM {BQ_TABLE_PATH}
WHERE date BETWEEN @start_date AND @end_date
GROUP BY date
ORDER BY date ASC
"""

HOUR_DOW_HEATMAP = f"""
WITH base AS (
    SELECT
        {_TS_EXPR} AS ts,
        indoor_temp, indoor_humidity, indoor_co2
    FROM {BQ_TABLE_PATH}
    WHERE date BETWEEN @start_date AND @end_date
)
SELECT
    EXTRACT(DAYOFWEEK FROM ts) AS dow,   -- 1 = Sunday … 7 = Saturday
    EXTRACT(HOUR FROM ts)      AS hour,
    AVG(indoor_temp)           AS indoor_temp,
    AVG(indoor_humidity)       AS indoor_humidity,
    AVG(indoor_co2)            AS indoor_co2
FROM base
GROUP BY dow, hour
ORDER BY dow, hour
"""

OUTDOOR_WEATHER_COUNTS = f"""
SELECT
    outdoor_weather AS description,
    COUNT(*) AS count
FROM {BQ_TABLE_PATH}
WHERE date BETWEEN @start_date AND @end_date
  AND outdoor_weather IS NOT NULL
GROUP BY description
ORDER BY count DESC
LIMIT 12
"""

AVAILABLE_DATE_RANGE = f"""
SELECT MIN(date) AS min_date, MAX(date) AS max_date
FROM {BQ_TABLE_PATH}
"""

"""Overview / landing page — live KPIs, 24h sparklines, outdoor & forecast cards."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st

from config import DEFAULT_LOCATION, REFRESH_INTERVAL_SECONDS
from components import charts, metrics
from data import bigquery_client, flask_api


def _safe_iloc(df: pd.DataFrame, idx: int, col: str):
    if df is None or df.empty or idx >= len(df):
        return None
    val = df.iloc[idx][col]
    return None if pd.isna(val) else val


def _format_last_seen(ts) -> str:
    if ts is None or pd.isna(ts):
        return "never"
    if not isinstance(ts, datetime):
        ts = pd.to_datetime(ts).to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def _outdoor_icon_url(icon_code: str) -> str:
    return f"https://openweathermap.org/img/wn/{icon_code}@2x.png"


@st.fragment(run_every=REFRESH_INTERVAL_SECONDS)
def _live_block() -> None:
    # Render the button BEFORE fetching data so that when it is clicked,
    # Streamlit reruns this fragment with a cleared cache, guaranteeing the
    # next fetch goes to BigQuery instead of returning the old cached value.
    top_left, top_right = st.columns([3, 1])
    with top_right:
        if st.button("Refresh now", width='stretch'):
            bigquery_client.fetch_latest_reading.clear()
            flask_api.get_recent_readings.clear()

    try:
        latest = bigquery_client.fetch_latest_reading()
    except Exception as exc:  # noqa: BLE001
        st.error(f"BigQuery unavailable: {type(exc).__name__}: {exc}")
        return

    if latest.empty:
        with top_left:
            st.info("No readings in BigQuery yet — waiting for the device to send its first sample.")
        return

    current = latest.iloc[0]
    previous_row = latest.iloc[1] if len(latest) > 1 else None
    prev = (lambda c: previous_row[c] if previous_row is not None and not pd.isna(previous_row[c]) else None)

    location = current.get("location") or DEFAULT_LOCATION
    last_ts = current.get("ts")

    with top_left:
        st.markdown(f"### Live conditions — {location}")
        st.caption(f"Last reading: {_format_last_seen(last_ts)} "
                   f"({pd.to_datetime(last_ts).strftime('%Y-%m-%d %H:%M:%S UTC')})")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metrics.kpi_card("Indoor temperature", current["indoor_temp"],
                         prev("indoor_temp"), suffix=" °C")
    with c2:
        metrics.kpi_card("Indoor humidity", current["indoor_humidity"],
                         prev("indoor_humidity"), suffix=" %")
    with c3:
        quality_text, _ = metrics.co2_quality_label(current["indoor_co2"])
        metrics.kpi_card("Indoor eCO₂", current["indoor_co2"],
                         prev("indoor_co2"), suffix=" ppm", ndigits=0,
                         help_text=f"Air quality: {quality_text}",
                         delta_color="inverse")
    with c4:
        metrics.kpi_card("Outdoor temperature", current["outdoor_temp"],
                         prev("outdoor_temp"), suffix=" °C")

    st.divider()

    # Outdoor live card + last-24h sparklines from Flask.
    outdoor_col, sparkline_col = st.columns([1, 2])
    with outdoor_col:
        st.markdown("#### Outdoor right now")
        outdoor = flask_api.get_current_outdoor(location)
        if outdoor:
            weather = (outdoor.get("weather") or [{}])[0]
            icon = weather.get("icon")
            description = (weather.get("description") or "").title()
            main = outdoor.get("main", {})

            icon_col, text_col = st.columns([1, 2])
            with icon_col:
                if icon:
                    st.image(_outdoor_icon_url(icon), width=96)
            with text_col:
                st.markdown(f"**{description or '—'}**")
                st.markdown(f"🌡️ {main.get('temp', '—')} °C "
                            f"(feels {main.get('feels_like', '—')} °C)")
                st.markdown(f"💧 {main.get('humidity', '—')} %")
        else:
            st.warning("Live outdoor data unavailable. Check Flask URL and shared secret.")

    with sparkline_col:
        st.markdown("#### Last 24 hours")
        # Fetched via Flask /recent-readings so it works independently of
        # any BigQuery date-boundary edge cases and adds no extra BQ cost.
        recent = flask_api.get_recent_readings(hours=24)
        if recent is None:
            st.caption("Recent readings unavailable. Check Flask URL and shared secret.")
        elif recent.empty:
            st.caption("No samples in the last 24 hours.")
        else:
            sp_cols = st.columns(3)
            specs = [
                ("indoor_temp", "Temperature (°C)", "#2E86AB"),
                ("indoor_humidity", "Humidity (%)", "#3FB28A"),
                ("indoor_co2", "eCO₂ (ppm)", "#C97064"),
            ]
            for col, (metric_key, label, color) in zip(sp_cols, specs):
                with col:
                    st.caption(label)
                    chart = charts.sparkline(recent, y_col=metric_key, color=color)
                    if chart is not None:
                        st.altair_chart(chart, width='stretch')
                    else:
                        st.write("—")


def _forecast_block(location: str) -> None:
    st.markdown("### 5-day forecast")
    forecast = flask_api.get_forecast(location)
    if not forecast:
        st.info("Forecast data unavailable. Configure `flask_shared_secret` to enable.")
        return

    items = forecast.get("list", []) or []
    if not items:
        st.info("Empty forecast payload from OpenWeather.")
        return

    # OpenWeather "/forecast" returns 3-hourly samples — collapse to daily min/max.
    df = pd.DataFrame([
        {
            "ts": pd.to_datetime(item["dt"], unit="s"),
            "temp": item["main"]["temp"],
            "icon": (item.get("weather") or [{}])[0].get("icon"),
            "description": (item.get("weather") or [{}])[0].get("description", "").title(),
        }
        for item in items
    ])
    df["date"] = df["ts"].dt.date
    grouped = (
        df.groupby("date")
        .agg(min_temp=("temp", "min"), max_temp=("temp", "max"),
             icon=("icon", lambda s: s.mode().iat[0] if not s.mode().empty else None),
             description=("description", lambda s: s.mode().iat[0] if not s.mode().empty else ""))
        .reset_index()
        .head(5)
    )

    cols = st.columns(len(grouped))
    for col, row in zip(cols, grouped.itertuples()):
        with col:
            st.markdown(f"**{row.date.strftime('%a %d %b')}**")
            if row.icon:
                st.image(_outdoor_icon_url(row.icon), width=72)
            st.markdown(f"{row.description}")
            st.markdown(f"⬆ {row.max_temp:.0f} °C &nbsp;&nbsp; ⬇ {row.min_temp:.0f} °C",
                        unsafe_allow_html=True)


def overview_page() -> None:
    st.title("🌤️  Home Weather Monitor")
    st.caption("Live indoor conditions, historical trends, and outdoor weather "
               "for your home weather station.")

    _live_block()
    st.divider()

    # Pull a fresh "current location" from BigQuery for the forecast lookup,
    # falling back to the configured default.
    try:
        latest = bigquery_client.fetch_latest_reading()
        location = (latest.iloc[0]["location"] if not latest.empty else None) or DEFAULT_LOCATION
    except Exception:  # noqa: BLE001
        location = DEFAULT_LOCATION

    _forecast_block(location)

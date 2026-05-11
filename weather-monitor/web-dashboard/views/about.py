"""About page — system explanation, schema docs, links."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from config import BQ_DATASET, BQ_TABLE, FLASK_BASE_URL, GCP_PROJECT_ID


_SCHEMA_ROWS = [
    ("date",             "STRING", "YYYY-MM-DD",   "M5Stack device clock"),
    ("time",             "STRING", "HH:MM:SS",     "M5Stack device clock"),
    ("indoor_temp",      "FLOAT",  "°C",           "ENV-III sensor on M5Stack"),
    ("indoor_humidity",  "FLOAT",  "%",            "ENV-III sensor on M5Stack"),
    ("indoor_co2",       "FLOAT",  "ppm (eCO₂)",   "TVOC/eCO₂ unit on M5Stack"),
    ("outdoor_temp",     "FLOAT",  "°C",           "OpenWeather, fetched by Flask on insert"),
    ("outdoor_humidity", "FLOAT",  "%",            "OpenWeather, fetched by Flask on insert"),
    ("outdoor_weather",  "STRING", "description",  "OpenWeather, fetched by Flask on insert"),
    ("location",         "STRING", "city name",    "IP geolocation reported by the device"),
]


def about_page() -> None:
    st.title("ℹ️  About this dashboard")

    st.markdown(
        """
        This dashboard is the **UI tier** of a three-tier system:

        1. **Device** — an M5Stack ESP32 with an ENV-III temperature/humidity
           sensor and a TVOC/eCO₂ unit posts a reading every few minutes.
        2. **Flask API** — receives each reading, enriches it with current
           outdoor weather from OpenWeather, and writes a row to BigQuery.
        3. **Streamlit dashboard** (this app) — queries BigQuery for history
           and the Flask service for live outdoor data and forecasts.

        Credentials are loaded from `st.secrets` for local / Streamlit Cloud
        deployments, and from Application Default Credentials (the attached
        service account) on Google Cloud Run. Nothing sensitive is hard-coded.
        """
    )

    st.subheader("BigQuery table")
    st.code(f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}", language="text")
    st.dataframe(
        pd.DataFrame(_SCHEMA_ROWS, columns=["Column", "Type", "Unit", "Source"]),
        hide_index=True,
        width='stretch',
    )

    st.subheader("Backend service")
    st.markdown(f"- Flask API: [`{FLASK_BASE_URL}`]({FLASK_BASE_URL})")
    st.markdown("- Outdoor weather: [OpenWeather](https://openweathermap.org/)")

    st.subheader("Refresh behaviour")
    st.markdown(
        """
        - **Latest reading** is refetched every minute (configurable via
          `refresh_interval_s`). The Overview page auto-refreshes that block
          without reloading the whole app.
        - **Historical range queries** are cached for 5 minutes.
        - **Daily summary and heatmap** are cached for 10 minutes.
        - **Live outdoor weather** is cached for 5 minutes.
        - **Forecast** is cached for 15 minutes.
        """
    )

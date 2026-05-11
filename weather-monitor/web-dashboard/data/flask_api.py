"""Thin client for the Flask weather service.

Used for live outdoor weather, 5-day forecast, and recent sensor readings so
the dashboard doesn't duplicate OpenWeather credentials or BigQuery access for
live data. Returns `None` on any failure; callers render a graceful placeholder.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
import requests
import streamlit as st

from config import FLASK_BASE_URL, FLASK_SHARED_SECRET

_TIMEOUT_S = 8


def _get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    if not FLASK_SHARED_SECRET:
        return None
    try:
        resp = requests.get(
            f"{FLASK_BASE_URL.rstrip('/')}{path}",
            headers={"X-Shared-Secret": FLASK_SHARED_SECRET},
            params=params or {},
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if payload.get("status") != "success":
        return None
    return payload


def _post(path: str, body: dict) -> Optional[dict]:
    if not FLASK_SHARED_SECRET:
        return None
    try:
        resp = requests.post(
            f"{FLASK_BASE_URL.rstrip('/')}{path}",
            json={"passwd": FLASK_SHARED_SECRET, **body},
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if payload.get("status") != "success":
        return None
    return payload


@st.cache_data(ttl=300, show_spinner=False)
def get_current_outdoor(city: str) -> Optional[dict]:
    """OpenWeather current-conditions payload (already unwrapped from envelope)."""
    payload = _post("/get_outdoor_weather", {"city": city})
    return payload["data"] if payload else None


@st.cache_data(ttl=900, show_spinner=False)
def get_forecast(city: str) -> Optional[dict]:
    """OpenWeather 5-day / 3-hour forecast payload."""
    payload = _post("/get_forecast", {"city": city})
    return payload["data"] if payload else None


@st.cache_data(ttl=120, show_spinner=False)
def get_recent_readings(hours: int = 24) -> Optional[pd.DataFrame]:
    """Sensor readings from the last `hours` hours via Flask /recent-readings.

    Returns a DataFrame with a `ts` column (datetime) sorted ascending,
    or None if the endpoint is unreachable / unconfigured.
    """
    payload = _get("/recent-readings", {"hours": hours})
    if payload is None:
        return None
    rows = payload.get("rows", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "date" in df.columns and "time" in df.columns:
        df["ts"] = pd.to_datetime(df["date"] + " " + df["time"], utc=True)
    df = df.sort_values("ts") if "ts" in df.columns else df
    return df

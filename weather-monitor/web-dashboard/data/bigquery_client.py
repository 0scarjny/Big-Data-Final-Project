"""BigQuery client factory + cached query helpers.

All public functions return `pandas.DataFrame`. Empty results return an empty
DataFrame with the expected columns so callers don't need to special-case.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd
import streamlit as st
from google.cloud import bigquery

from config import GCP_PROJECT_ID, load_gcp_credentials
from data import queries


@st.cache_resource(show_spinner=False)
def get_client() -> bigquery.Client:
    creds = load_gcp_credentials()
    if creds is not None:
        return bigquery.Client(project=GCP_PROJECT_ID, credentials=creds)
    return bigquery.Client(project=GCP_PROJECT_ID)


def _date_params(start: date, end: date) -> list[bigquery.ScalarQueryParameter]:
    return [
        bigquery.ScalarQueryParameter("start_date", "STRING", start.isoformat()),
        bigquery.ScalarQueryParameter("end_date", "STRING", end.isoformat()),
    ]


def _run(sql: str, params: Optional[list] = None) -> pd.DataFrame:
    job_config = bigquery.QueryJobConfig(query_parameters=params or [])
    job = get_client().query(sql, job_config=job_config)
    return job.result().to_dataframe(create_bqstorage_client=False)


@st.cache_data(ttl=60, show_spinner=False)
def fetch_latest_reading() -> pd.DataFrame:
    """Most recent two rows (newest first) — second row used for delta values."""
    return _run(queries.LATEST_READING)


@st.cache_data(ttl=300, show_spinner=False)
def fetch_range(start: date, end: date) -> pd.DataFrame:
    return _run(queries.RANGE_READINGS, _date_params(start, end))


@st.cache_data(ttl=600, show_spinner=False)
def fetch_hourly_aggregates(start: date, end: date) -> pd.DataFrame:
    return _run(queries.HOURLY_AGGREGATES, _date_params(start, end))


@st.cache_data(ttl=600, show_spinner=False)
def fetch_daily_summary(start: date, end: date) -> pd.DataFrame:
    return _run(queries.DAILY_SUMMARY, _date_params(start, end))


@st.cache_data(ttl=600, show_spinner=False)
def fetch_hour_dow_heatmap(start: date, end: date) -> pd.DataFrame:
    return _run(queries.HOUR_DOW_HEATMAP, _date_params(start, end))


@st.cache_data(ttl=600, show_spinner=False)
def fetch_outdoor_weather_counts(start: date, end: date) -> pd.DataFrame:
    return _run(queries.OUTDOOR_WEATHER_COUNTS, _date_params(start, end))


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_available_date_range() -> tuple[Optional[date], Optional[date]]:
    df = _run(queries.AVAILABLE_DATE_RANGE)
    if df.empty:
        return None, None
    row = df.iloc[0]
    min_d = pd.to_datetime(row["min_date"]).date() if pd.notna(row["min_date"]) else None
    max_d = pd.to_datetime(row["max_date"]).date() if pd.notna(row["max_date"]) else None
    return min_d, max_d

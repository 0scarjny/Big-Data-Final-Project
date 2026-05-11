"""Shared sidebar filter widgets."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import streamlit as st

from data import bigquery_client


def date_range_picker(
    key_prefix: str,
    *,
    default_days: int = 7,
) -> tuple[date, date]:
    """Date range picker bounded by the table's actual data range.

    Defaults to the last `default_days` days, clipped to the available range.
    Returns `(start, end)` as `date` objects (always start ≤ end).
    """
    min_d, max_d = bigquery_client.fetch_available_date_range()
    today = date.today()
    if max_d is None:
        max_d = today
    if min_d is None:
        min_d = max_d - timedelta(days=30)

    default_end = max_d
    default_start = max(min_d, default_end - timedelta(days=default_days - 1))

    selection = st.date_input(
        "Date range",
        value=(default_start, default_end),
        min_value=min_d,
        max_value=max_d,
        key=f"{key_prefix}_date_range",
        format="YYYY-MM-DD",
    )
    if isinstance(selection, tuple) and len(selection) == 2:
        start, end = selection
    else:
        # User picked a single date — treat as a one-day range.
        start = end = selection if isinstance(selection, date) else default_start
    if start > end:
        start, end = end, start
    return start, end


def metric_multiselect(key: str) -> list[str]:
    options = {
        "Indoor temperature": "indoor_temp",
        "Indoor humidity": "indoor_humidity",
        "Indoor eCO₂": "indoor_co2",
    }
    chosen = st.multiselect(
        "Metrics",
        list(options.keys()),
        default=list(options.keys()),
        key=key,
    )
    return [options[label] for label in chosen]


def aggregation_toggle(key: str) -> str:
    return st.radio(
        "Resolution",
        ["Raw readings", "Hourly averages"],
        horizontal=True,
        key=key,
    )

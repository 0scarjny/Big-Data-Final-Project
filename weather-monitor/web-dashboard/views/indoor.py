"""Indoor history page — date range, charts, daily summary, CSV export."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from components import charts, filters
from data import bigquery_client

_METRIC_META = {
    "indoor_temp":     {"title": "Indoor temperature", "unit": "°C",  "color": "#2E86AB"},
    "indoor_humidity": {"title": "Indoor humidity",    "unit": "%",   "color": "#3FB28A"},
    "indoor_co2":      {"title": "Indoor eCO₂",        "unit": "ppm", "color": "#C97064"},
}


def _summary_for_display(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.date
    rounders = {
        "indoor_temp_min": 1, "indoor_temp_avg": 1, "indoor_temp_max": 1,
        "indoor_humidity_min": 1, "indoor_humidity_avg": 1, "indoor_humidity_max": 1,
        "indoor_co2_min": 0, "indoor_co2_avg": 0, "indoor_co2_max": 0,
    }
    for col, decimals in rounders.items():
        if col in out.columns:
            out[col] = out[col].round(decimals)
    rename = {
        "date": "Date",
        "indoor_temp_min": "Temp min (°C)",
        "indoor_temp_avg": "Temp avg (°C)",
        "indoor_temp_max": "Temp max (°C)",
        "indoor_humidity_min": "Humid. min (%)",
        "indoor_humidity_avg": "Humid. avg (%)",
        "indoor_humidity_max": "Humid. max (%)",
        "indoor_co2_min": "eCO₂ min (ppm)",
        "indoor_co2_avg": "eCO₂ avg (ppm)",
        "indoor_co2_max": "eCO₂ max (ppm)",
        "sample_count": "Samples",
    }
    return out.rename(columns=rename)


def indoor_page() -> None:
    st.title("📊  Indoor History")
    st.caption("Browse historical indoor temperature, humidity, and eCO₂ samples.")

    with st.sidebar:
        st.header("Filters")
        start, end = filters.date_range_picker("indoor", default_days=7)
        chosen_metrics = filters.metric_multiselect("indoor_metrics")
        resolution = filters.aggregation_toggle("indoor_resolution")

    if not chosen_metrics:
        st.warning("Pick at least one metric in the sidebar.")
        return

    try:
        if resolution == "Hourly averages":
            data = bigquery_client.fetch_hourly_aggregates(start, end)
        else:
            data = bigquery_client.fetch_range(start, end)
    except Exception as exc:  # noqa: BLE001
        st.error(f"BigQuery query failed: {type(exc).__name__}: {exc}")
        return

    if data.empty:
        st.info("No data in this range.")
        return

    for metric in chosen_metrics:
        meta = _METRIC_META[metric]
        st.subheader(f"{meta['title']} ({meta['unit']})")
        chart = charts.line_chart(
            data,
            y_col=metric,
            y_title=meta["title"],
            y_unit=meta["unit"],
            color=meta["color"],
        )
        st.altair_chart(chart, width='stretch')

    st.divider()
    st.subheader("Daily summary")
    try:
        summary = bigquery_client.fetch_daily_summary(start, end)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not load daily summary: {type(exc).__name__}: {exc}")
        return

    if summary.empty:
        st.info("No samples to summarise.")
    else:
        st.dataframe(_summary_for_display(summary), hide_index=True, width='stretch')

    st.divider()
    st.subheader("Export")
    csv_bytes = data.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download current view as CSV",
        data=csv_bytes,
        file_name=f"indoor_{start.isoformat()}_{end.isoformat()}_{resolution.lower().replace(' ', '_')}.csv",
        mime="text/csv",
    )

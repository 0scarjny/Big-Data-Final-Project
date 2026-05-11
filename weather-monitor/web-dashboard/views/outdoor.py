"""Outdoor / comparison page — indoor-vs-outdoor charts + heatmap + weather mix."""
from __future__ import annotations

import streamlit as st

from components import charts, filters
from data import bigquery_client


def outdoor_page() -> None:
    st.title("☁️  Outdoor & Comparison")
    st.caption("Compare indoor readings against the outdoor conditions stored "
               "with each sample, and explore patterns by hour of day.")

    with st.sidebar:
        st.header("Filters")
        start, end = filters.date_range_picker("outdoor", default_days=14)
        heatmap_metric_label = st.selectbox(
            "Heatmap metric",
            ["Indoor temperature", "Indoor humidity", "Indoor eCO₂"],
            index=0,
            key="outdoor_heatmap_metric",
        )

    try:
        hourly = bigquery_client.fetch_hourly_aggregates(start, end)
    except Exception as exc:  # noqa: BLE001
        st.error(f"BigQuery query failed: {type(exc).__name__}: {exc}")
        return

    if hourly.empty:
        st.info("No samples in this range yet.")
        return

    st.subheader("Indoor vs outdoor temperature")
    st.altair_chart(
        charts.comparison_chart(
            hourly, indoor_col="indoor_temp", outdoor_col="outdoor_temp",
            y_title="Temperature", y_unit="°C",
        ),
        width='stretch',
    )

    st.subheader("Indoor vs outdoor humidity")
    st.altair_chart(
        charts.comparison_chart(
            hourly, indoor_col="indoor_humidity", outdoor_col="outdoor_humidity",
            y_title="Humidity", y_unit="%",
        ),
        width='stretch',
    )

    st.divider()
    st.subheader("Pattern by hour of day × day of week")
    try:
        grid = bigquery_client.fetch_hour_dow_heatmap(start, end)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Heatmap query failed: {type(exc).__name__}: {exc}")
        return

    metric_map = {
        "Indoor temperature": ("indoor_temp", "Avg °C", "viridis"),
        "Indoor humidity":    ("indoor_humidity", "Avg %", "blues"),
        "Indoor eCO₂":        ("indoor_co2", "Avg ppm", "reds"),
    }
    value_col, color_title, scheme = metric_map[heatmap_metric_label]

    if grid.empty or grid[value_col].dropna().empty:
        st.info("Not enough samples to build the heatmap yet.")
    else:
        st.altair_chart(
            charts.heatmap(grid, value_col=value_col, title=color_title, scheme=scheme),
            width='stretch',
        )

    st.divider()
    st.subheader("Outdoor weather mix")
    st.caption("How often each outdoor weather description was recorded with a sample.")
    try:
        counts = bigquery_client.fetch_outdoor_weather_counts(start, end)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Weather-mix query failed: {type(exc).__name__}: {exc}")
        return

    if counts.empty:
        st.info("No outdoor descriptions recorded.")
    else:
        st.altair_chart(charts.description_bar(counts), width='stretch')

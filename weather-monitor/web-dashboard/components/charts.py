"""Altair chart builders. All return chart objects so callers can pipe them
into `st.altair_chart(..., use_container_width=True)` without coupling on
streamlit display side-effects.
"""
from __future__ import annotations

from typing import Optional

import altair as alt
import pandas as pd

_DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def line_chart(
    df: pd.DataFrame,
    *,
    y_col: str,
    y_title: str,
    y_unit: str = "",
    color: str = "#2E86AB",
    height: int = 280,
) -> alt.Chart:
    """Single-metric time series with hover tooltip and zoom."""
    plot_df = df.dropna(subset=[y_col]).copy()
    return (
        alt.Chart(plot_df)
        .mark_line(color=color, strokeWidth=2)
        .encode(
            x=alt.X("ts:T", title="Time"),
            y=alt.Y(f"{y_col}:Q", title=f"{y_title}{(' (' + y_unit + ')') if y_unit else ''}",
                    scale=alt.Scale(zero=False)),
            tooltip=[
                alt.Tooltip("ts:T", title="Time", format="%Y-%m-%d %H:%M"),
                alt.Tooltip(f"{y_col}:Q", title=y_title, format=".1f"),
            ],
        )
        .properties(height=height)
        .interactive(bind_y=False)
    )


def comparison_chart(
    df: pd.DataFrame,
    *,
    indoor_col: str,
    outdoor_col: str,
    y_title: str,
    y_unit: str = "",
    height: int = 320,
) -> alt.Chart:
    """Indoor vs outdoor overlay on a single Y axis."""
    keep = ["ts", indoor_col, outdoor_col]
    long = (
        df[keep]
        .melt(id_vars="ts", var_name="series", value_name="value")
        .dropna(subset=["value"])
    )
    long["series"] = long["series"].map({indoor_col: "Indoor", outdoor_col: "Outdoor"})
    return (
        alt.Chart(long)
        .mark_line(strokeWidth=2)
        .encode(
            x=alt.X("ts:T", title="Time"),
            y=alt.Y("value:Q", title=f"{y_title}{(' (' + y_unit + ')') if y_unit else ''}",
                    scale=alt.Scale(zero=False)),
            color=alt.Color(
                "series:N",
                title="",
                scale=alt.Scale(domain=["Indoor", "Outdoor"], range=["#2E86AB", "#E07A5F"]),
            ),
            tooltip=[
                alt.Tooltip("ts:T", title="Time", format="%Y-%m-%d %H:%M"),
                alt.Tooltip("series:N", title=""),
                alt.Tooltip("value:Q", title="Value", format=".1f"),
            ],
        )
        .properties(height=height)
        .interactive(bind_y=False)
    )


def heatmap(df: pd.DataFrame, *, value_col: str, title: str, scheme: str = "viridis") -> alt.Chart:
    """Hour-of-day × day-of-week heatmap."""
    plot = df.copy()
    plot["dow_label"] = plot["dow"].astype(int).map(lambda i: _DOW_LABELS[i - 1])
    return (
        alt.Chart(plot)
        .mark_rect()
        .encode(
            x=alt.X("hour:O", title="Hour of day"),
            y=alt.Y("dow_label:N", title="Day of week", sort=_DOW_LABELS),
            color=alt.Color(f"{value_col}:Q", title=title, scale=alt.Scale(scheme=scheme)),
            tooltip=[
                alt.Tooltip("dow_label:N", title="Day"),
                alt.Tooltip("hour:O", title="Hour"),
                alt.Tooltip(f"{value_col}:Q", title=title, format=".1f"),
            ],
        )
        .properties(height=260)
    )


def description_bar(df: pd.DataFrame) -> alt.Chart:
    return (
        alt.Chart(df)
        .mark_bar(color="#2E86AB")
        .encode(
            x=alt.X("count:Q", title="Samples"),
            y=alt.Y("description:N", sort="-x", title=""),
            tooltip=[
                alt.Tooltip("description:N", title="Outdoor weather"),
                alt.Tooltip("count:Q", title="Samples"),
            ],
        )
        .properties(height=max(120, 24 * len(df)))
    )


def sparkline(df: pd.DataFrame, *, y_col: str, color: str = "#2E86AB", height: int = 80) -> Optional[alt.Chart]:
    plot_df = df.dropna(subset=[y_col]).copy()
    if plot_df.empty:
        return None
    return (
        alt.Chart(plot_df)
        .mark_area(color=color, opacity=0.18, line={"color": color, "strokeWidth": 2})
        .encode(
            x=alt.X("ts:T", axis=None),
            y=alt.Y(f"{y_col}:Q", axis=None, scale=alt.Scale(zero=False)),
            tooltip=[
                alt.Tooltip("ts:T", title="Time", format="%H:%M"),
                alt.Tooltip(f"{y_col}:Q", title="Value", format=".1f"),
            ],
        )
        .properties(height=height)
    )

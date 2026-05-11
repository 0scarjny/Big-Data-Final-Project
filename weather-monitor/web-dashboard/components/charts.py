"""Altair chart builders. All return chart objects so callers can pipe them
into `st.altair_chart(..., use_container_width=True)` without coupling on
streamlit display side-effects.
"""
from __future__ import annotations

from typing import Optional

import altair as alt
import pandas as pd

_DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# A gap longer than this triggers a dashed bridge in time-series charts.
# Picked to comfortably exceed the device's normal upload interval (a few
# minutes) AND the hourly-aggregate bucket size (60 min) so we don't false-
# positive on perfectly normal data. Callers can override per chart.
_DEFAULT_GAP_MINUTES = 90


def _segment_and_bridges(
    df: pd.DataFrame,
    *,
    ts_col: str = "ts",
    value_col: str,
    series: str = "",
    gap_minutes: int = _DEFAULT_GAP_MINUTES,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a series into solid segments + dashed gap bridges.

    Returns `(segments_df, bridges_df)` where:
      - `segments_df` has the original `(ts, value)` rows plus a `segment_id`
        column. Each run of consecutive readings within `gap_minutes` of each
        other shares an id; ids increment at each gap. Altair's `detail`
        encoding on `segment_id` draws each segment as its own line, so the
        solid line breaks at gaps instead of bridging them.
      - `bridges_df` has two rows per gap (the last point before and first
        point after), keyed by a unique `gap_id` so the dashed layer renders
        each bridge as a separate dashed segment.

    Both dataframes carry a `series` column so multi-series charts can pass them
    through a single Altair layer with `color="series:N"` for the legend.
    """
    clean = (
        df[[ts_col, value_col]]
        .dropna(subset=[value_col])
        .sort_values(ts_col)
        .reset_index(drop=True)
        .copy()
    )
    if clean.empty:
        empty_seg = pd.DataFrame(columns=[ts_col, value_col, "series", "segment_id"])
        empty_br = pd.DataFrame(columns=[ts_col, value_col, "series", "gap_id"])
        return empty_seg, empty_br

    threshold = pd.Timedelta(minutes=gap_minutes)
    is_gap = clean[ts_col].diff() > threshold
    segment_idx = is_gap.cumsum().astype(int)

    segments = clean.copy()
    segments["series"] = series
    segments["segment_id"] = (
        (f"{series}_" if series else "") + segment_idx.astype(str)
    )

    gap_positions = clean.index[is_gap].tolist()
    bridge_rows = []
    for i, idx in enumerate(gap_positions):
        gap_id = f"{series}_{i}" if series else str(i)
        for row in (clean.iloc[idx - 1], clean.iloc[idx]):
            bridge_rows.append({
                ts_col: row[ts_col],
                value_col: row[value_col],
                "series": series,
                "gap_id": gap_id,
            })
    bridges = (
        pd.DataFrame(bridge_rows)
        if bridge_rows
        else pd.DataFrame(columns=[ts_col, value_col, "series", "gap_id"])
    )
    return segments, bridges


def line_chart(
    df: pd.DataFrame,
    *,
    y_col: str,
    y_title: str,
    y_unit: str = "",
    color: str = "#2E86AB",
    height: int = 280,
    gap_minutes: int = _DEFAULT_GAP_MINUTES,
) -> alt.LayerChart:
    """Single-metric time series. Gaps render as dashed bridges; the solid line
    breaks at each gap so straight cross-gap segments never appear."""
    y_label = f"{y_title}{(' (' + y_unit + ')') if y_unit else ''}"

    segments, bridges = _segment_and_bridges(
        df, value_col=y_col, gap_minutes=gap_minutes,
    )

    solid = (
        alt.Chart(segments)
        .mark_line(color=color, strokeWidth=2)
        .encode(
            x=alt.X("ts:T", title="Time"),
            y=alt.Y(f"{y_col}:Q", title=y_label, scale=alt.Scale(zero=False)),
            detail=alt.Detail("segment_id:N"),
            tooltip=[
                alt.Tooltip("ts:T", title="Time", format="%Y-%m-%d %H:%M"),
                alt.Tooltip(f"{y_col}:Q", title=y_title, format=".1f"),
            ],
        )
    )

    layers = [solid]
    if not bridges.empty:
        dashed = (
            alt.Chart(bridges)
            .mark_line(strokeDash=[6, 4], strokeWidth=1.5, color=color, opacity=0.55)
            .encode(
                x=alt.X("ts:T"),
                y=alt.Y(f"{y_col}:Q", scale=alt.Scale(zero=False)),
                detail=alt.Detail("gap_id:N"),
                tooltip=[
                    alt.Tooltip("ts:T", title="Time", format="%Y-%m-%d %H:%M"),
                    alt.Tooltip(f"{y_col}:Q", title="No data (gap)", format=".1f"),
                ],
            )
        )
        layers.append(dashed)

    return (
        alt.layer(*layers)
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
    gap_minutes: int = _DEFAULT_GAP_MINUTES,
) -> alt.LayerChart:
    """Indoor vs outdoor overlay. Solid line breaks at each gap; gaps render as
    dashed bridges per series."""
    y_label = f"{y_title}{(' (' + y_unit + ')') if y_unit else ''}"
    color_domain = ["Indoor", "Outdoor"]
    color_range = ["#2E86AB", "#E07A5F"]
    color_scale = alt.Scale(domain=color_domain, range=color_range)

    col_map = {indoor_col: "Indoor", outdoor_col: "Outdoor"}

    segment_frames, bridge_frames = [], []
    for col, name in col_map.items():
        sub = df[["ts", col]].rename(columns={col: "value"})
        seg, br = _segment_and_bridges(
            sub, value_col="value", series=name, gap_minutes=gap_minutes,
        )
        segment_frames.append(seg)
        bridge_frames.append(br)

    segments = pd.concat(segment_frames, ignore_index=True)
    bridges = pd.concat(bridge_frames, ignore_index=True)

    solid = (
        alt.Chart(segments)
        .mark_line(strokeWidth=2)
        .encode(
            x=alt.X("ts:T", title="Time"),
            y=alt.Y("value:Q", title=y_label, scale=alt.Scale(zero=False)),
            color=alt.Color("series:N", title="", scale=color_scale),
            detail=alt.Detail("segment_id:N"),
            tooltip=[
                alt.Tooltip("ts:T", title="Time", format="%Y-%m-%d %H:%M"),
                alt.Tooltip("series:N", title=""),
                alt.Tooltip("value:Q", title="Value", format=".1f"),
            ],
        )
    )

    layers = [solid]
    if not bridges.empty:
        dashed = (
            alt.Chart(bridges)
            .mark_line(strokeDash=[6, 4], strokeWidth=1.5, opacity=0.55)
            .encode(
                x=alt.X("ts:T"),
                y=alt.Y("value:Q", scale=alt.Scale(zero=False)),
                color=alt.Color("series:N", title="", scale=color_scale),
                detail=alt.Detail("gap_id:N"),
                tooltip=[
                    alt.Tooltip("ts:T", title="Time", format="%Y-%m-%d %H:%M"),
                    alt.Tooltip("series:N", title=""),
                    alt.Tooltip("value:Q", title="No data (gap)", format=".1f"),
                ],
            )
        )
        layers.append(dashed)

    return (
        alt.layer(*layers)
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

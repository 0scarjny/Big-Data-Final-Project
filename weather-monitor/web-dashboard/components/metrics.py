"""Reusable KPI/metric card builders."""
from __future__ import annotations

from typing import Optional

import pandas as pd
import streamlit as st


def _fmt(value, suffix: str, ndigits: int = 1) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):.{ndigits}f}{suffix}"


def _delta(current, previous, ndigits: int = 1) -> Optional[str]:
    if current is None or previous is None or pd.isna(current) or pd.isna(previous):
        return None
    diff = float(current) - float(previous)
    if abs(diff) < 10 ** (-ndigits):
        return None
    sign = "+" if diff > 0 else ""
    return f"{sign}{diff:.{ndigits}f}"


def kpi_card(label: str, value, previous=None, *, suffix: str = "", ndigits: int = 1,
             help_text: Optional[str] = None, delta_color: str = "normal") -> None:
    st.metric(
        label=label,
        value=_fmt(value, suffix, ndigits),
        delta=_delta(value, previous, ndigits),
        delta_color=delta_color,
        help=help_text,
    )


def co2_quality_label(co2_ppm) -> tuple[str, str]:
    """ASHRAE-ish bands: returns (text, status color hint)."""
    if co2_ppm is None or pd.isna(co2_ppm):
        return "Unknown", "off"
    v = float(co2_ppm)
    if v < 800:
        return "Excellent", "good"
    if v < 1000:
        return "Good", "good"
    if v < 1500:
        return "Moderate", "warn"
    return "Poor — ventilate", "bad"

"""Entry point for the home weather monitoring dashboard."""
from __future__ import annotations

import streamlit as st

from views import about, indoor, outdoor, overview


st.set_page_config(
    page_title="Home Weather Monitor",
    page_icon="🌤️",
    layout="wide",
    initial_sidebar_state="expanded",
)


pages = [
    st.Page(overview.overview_page, title="Overview",             icon=":material/home:", default=True),
    st.Page(indoor.indoor_page,     title="Indoor History",       icon=":material/thermostat:"),
    st.Page(outdoor.outdoor_page,   title="Outdoor & Comparison", icon=":material/cloud:"),
    st.Page(about.about_page,       title="About",                icon=":material/info:"),
]

st.navigation(pages).run()

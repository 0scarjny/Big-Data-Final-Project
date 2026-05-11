"""Centralised configuration loader for the Streamlit dashboard.

Resolution order for every setting: `st.secrets` (Streamlit Cloud / local) →
environment variable (Cloud Run) → hard-coded default. Nothing in this module
contains real credentials.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

import streamlit as st
from google.oauth2 import service_account


def _from_secrets(section: str, key: str) -> Optional[str]:
    """Read `st.secrets[section][key]` without raising if missing."""
    try:
        return st.secrets[section][key]
    except (KeyError, FileNotFoundError, AttributeError):
        return None


def _resolve(section: str, key: str, env_var: str, default: Optional[str] = None) -> Optional[str]:
    value = _from_secrets(section, key)
    if value is not None and value != "":
        return value
    value = os.environ.get(env_var)
    if value is not None and value != "":
        return value
    return default


GCP_PROJECT_ID = _resolve("app", "gcp_project_id", "GCP_PROJECT_ID", "data-buckets-489022")
BQ_DATASET = _resolve("app", "bq_dataset", "BQ_DATASET", "weather_records")
BQ_TABLE = _resolve("app", "bq_table", "BQ_TABLE", "weather-data")
BQ_TABLE_PATH = f"`{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}`"

FLASK_BASE_URL = _resolve(
    "app", "flask_base_url", "FLASK_BASE_URL",
    "https://flask-app-868833155300.europe-west6.run.app",
)
FLASK_SHARED_SECRET = _resolve("app", "flask_shared_secret", "FLASK_SHARED_SECRET", "")

DEFAULT_LOCATION = _resolve("app", "default_location", "DEFAULT_LOCATION", "Lausanne")

try:
    REFRESH_INTERVAL_SECONDS = int(
        _resolve("app", "refresh_interval_s", "REFRESH_INTERVAL_S", "60") or "60"
    )
except ValueError:
    REFRESH_INTERVAL_SECONDS = 60


@lru_cache(maxsize=1)
def load_gcp_credentials() -> Optional[service_account.Credentials]:
    """Return service-account credentials if a JSON key is supplied via
    `st.secrets["gcp_service_account"]`, else `None` so the BigQuery client
    falls back to Application Default Credentials (Cloud Run's attached SA).
    """
    try:
        info = st.secrets["gcp_service_account"]
    except (KeyError, FileNotFoundError, AttributeError):
        return None

    info_dict = dict(info)
    if not info_dict.get("private_key") or not info_dict.get("client_email"):
        return None

    return service_account.Credentials.from_service_account_info(info_dict)

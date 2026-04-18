import json
import os

from dotenv import find_dotenv, load_dotenv
from google.cloud import bigquery
from google.oauth2 import service_account

load_dotenv(find_dotenv())

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "data-buckets-489022")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "weather_records")
WEATHER_TABLE_NAME = os.environ.get("WEATHER_TABLE_NAME", "weather-data")
WEATHER_TABLE_PATH = f"{PROJECT_ID}.{DATABASE_NAME}.{WEATHER_TABLE_NAME}"

# TODO: move to Secret Manager (see secret_manager.py) before production.
PASSWORD_HASH = "2323232dsdasfdafgtgsfa9034G@"


def get_bigquery_client(project):
    """1) GOOGLE_SERVICE_ACCOUNT_JSON env var (local dev via .env)
    2) Application Default Credentials (Cloud Run's attached SA)
    """
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        credentials = service_account.Credentials.from_service_account_info(json.loads(raw_json))
        return bigquery.Client(project=project, credentials=credentials)
    return bigquery.Client(project=project)


client = get_bigquery_client(PROJECT_ID)

# Startup probe: used by the insert route to check each column's dtype.
df = client.query(f"SELECT * FROM `{WEATHER_TABLE_PATH}` LIMIT 10").to_dataframe()

import json
import os

from dotenv import find_dotenv, load_dotenv
from google.cloud import bigquery, speech, texttospeech
from google.oauth2 import service_account

try:
    from src.secret_manager import access_secret_version
except ImportError:
    from secret_manager import access_secret_version

load_dotenv(find_dotenv())

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "data-buckets-489022")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "weather_records")
WEATHER_TABLE_NAME = os.environ.get("WEATHER_TABLE_NAME", "weather-data")
WEATHER_TABLE_PATH = f"{PROJECT_ID}.{DATABASE_NAME}.{WEATHER_TABLE_NAME}"
OPEN_WEATHER_SECRET_ID = os.environ.get("OPEN_WEATHER_SECRET_ID", "OPEN_WEATHER_API_KEY")

# TODO: move to Secret Manager (see secret_manager.py) before production.
PASSWORD_HASH = "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4"


def get_open_weather_api_key():
    """Local dev: read from .env. Cloud: fetch from Secret Manager."""
    key = os.environ.get("OPEN_WEATHER_API_KEY")
    if key:
        print("Using OpenWeather API key from environment variable.")
        return key

    key = access_secret_version(PROJECT_ID, OPEN_WEATHER_SECRET_ID)
    if key:
        print("Using OpenWeather API key from Secret Manager.")
        return key
    

    raise RuntimeError(
        f"Could not load OpenWeather API key: not in env and "
        f"Secret Manager lookup for {OPEN_WEATHER_SECRET_ID!r} failed. "
        "Check IAM permissions and that the secret exists."
    )
API_KEY = get_open_weather_api_key()


def _service_account_credentials():
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        return service_account.Credentials.from_service_account_info(json.loads(raw_json))
    return None


def get_bigquery_client(project):
    """1) GOOGLE_SERVICE_ACCOUNT_JSON env var (local dev via .env)
    2) Application Default Credentials (Cloud Run's attached SA)
    """
    credentials = _service_account_credentials()
    if credentials is not None:
        return bigquery.Client(project=project, credentials=credentials)
    return bigquery.Client(project=project)


def get_speech_client():
    credentials = _service_account_credentials()
    if credentials is not None:
        return speech.SpeechClient(credentials=credentials)
    return speech.SpeechClient()


def get_tts_client():
    credentials = _service_account_credentials()
    if credentials is not None:
        return texttospeech.TextToSpeechClient(credentials=credentials)
    return texttospeech.TextToSpeechClient()


client = get_bigquery_client(PROJECT_ID)
speech_client = get_speech_client()
tts_client = get_tts_client()

# Vertex AI region for Gemini. europe-west6 (where Cloud Run lives) is NOT a
# Vertex location, so we default to us-central1 — overridable via env if you
# want europe-west1/europe-west4 for lower latency.
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")


def get_vertex_credentials():
    """Returns (credentials, project_id) usable by google-genai's Vertex mode.

    Service-account credentials built from a JSON key carry no scopes by
    default; Vertex AI rejects them with 'invalid_scope'. Attach cloud-platform
    explicitly. (BigQuery/Speech/TTS clients auto-scope, the genai SDK does not.)
    """
    creds = _service_account_credentials()
    if creds is not None:
        creds = creds.with_scopes(["https://www.googleapis.com/auth/cloud-platform"])
    return creds, PROJECT_ID

# Startup probe: used by the insert route to check each column's dtype.
df = client.query(f"SELECT * FROM `{WEATHER_TABLE_PATH}` LIMIT 10").to_dataframe()

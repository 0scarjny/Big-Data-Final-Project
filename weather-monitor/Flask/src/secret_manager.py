import json
import logging
import os

import google_crc32c
from google.cloud import secretmanager
from google.oauth2 import service_account

logger = logging.getLogger(__name__)


def get_secret_manager_client():
    """
    Returns a Secret Manager client.
    Preference order:
    1) GOOGLE_SERVICE_ACCOUNT_JSON env var
    2) Application Default Credentials
    """
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        logger.debug("Using GOOGLE_SERVICE_ACCOUNT_JSON environment variable")
        credentials = service_account.Credentials.from_service_account_info(json.loads(raw_json))
        return secretmanager.SecretManagerServiceClient(credentials=credentials)

    logger.debug("Using Application Default Credentials (GCP Environment)")
    return secretmanager.SecretManagerServiceClient()


def access_secret_version(project_id, secret_id):
    """Access the secret version using the auto-detected client."""
    client = get_secret_manager_client()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"

    try:
        response = client.access_secret_version(request={"name": name})

        crc32c = google_crc32c.Checksum()
        crc32c.update(response.payload.data)
        if response.payload.data_crc32c != int(crc32c.hexdigest(), 16):
            raise Exception("Data corruption detected.")

        return response.payload.data.decode("UTF-8")

    except Exception as e:
        logger.debug(f"Error accessing secret {secret_id!r}: {e}")
        return None


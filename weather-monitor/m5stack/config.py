# Config file with passwords and urls for the API

BASE_URL = 'https://flask-app-868833155300.europe-west6.run.app'

BIGQUERY_URL     = BASE_URL + '/send-to-bigquery'
VOICE_URL        = BASE_URL + '/voice-assistant'
FORECAST_URL     = BASE_URL + '/get_forecast'
ANNOUNCEMENT_URL = BASE_URL + '/critical-announcement'


# Centralised secrets. TODO: rotate and externalize — the previous values
# were committed to git history across cloud.py, forecast.py, voice_client.py
# and voice_test.py, so they should be considered public until rotated.

SHARED_SECRET = '03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4'
IPDATA_KEY = "e2f1b4d9820c7256c8ccf858c57a98d71319a84664c01d23886f1ef6"

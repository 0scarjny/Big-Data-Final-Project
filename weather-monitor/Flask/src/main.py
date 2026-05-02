import json
import os

import requests
from flask import Flask, Response, request
from google.cloud import bigquery

try:
    from src.clients import PASSWORD_HASH, WEATHER_TABLE_PATH, client, df, API_KEY
    from src import openweather, voice, actions
    from src.logger import get_logger, setup_logging
except ImportError:
    from clients import PASSWORD_HASH, WEATHER_TABLE_PATH, client, df, API_KEY
    import openweather
    import voice
    import actions
    from logger import get_logger, setup_logging

setup_logging()
log = get_logger("voice_assistant.main")
app = Flask(__name__)


def _require_auth(payload):
    if payload.get("passwd") != PASSWORD_HASH:
        return {"status": "failed", "error": "Incorrect Password!"}, 401
    return None


@app.route("/send-to-bigquery", methods=["GET", "POST"])
def send_to_bigquery():
    if request.method == "POST":
        return _bigquery_insert()
    return _bigquery_query()


def _bigquery_insert():
    """POST: insert one sensor reading + outdoor weather into BigQuery."""
    payload = request.get_json(force=True)
    auth_err = _require_auth(payload)
    if auth_err:
        return auth_err

    local_values = payload.get("values", {})
    location = payload.get("location", "Lausanne")

    try:
        owm_data = openweather.fetch_current(location)
        if owm_data is None:
            return {"status": "failed", "error": f"City not found: {location}"}, 404

        row_to_insert = {
            **local_values,
            "outdoor_temp": owm_data["main"]["temp"],
            "outdoor_humidity": owm_data["main"]["humidity"],
            "outdoor_weather": owm_data["weather"][0]["description"],
            "location": location,
        }

        errors = client.insert_rows_json(WEATHER_TABLE_PATH, [row_to_insert])
        if errors:
            return {"status": "failed", "error": f"BigQuery Insert Error: {errors}"}, 500

        return {"status": "success", "inserted_data": row_to_insert}, 200

    except requests.exceptions.RequestException as e:
        return {"status": "failed", "error": f"External API Error: {str(e)}"}, 502
    except Exception as e:
        return {"status": "failed", "error": str(e)}, 500


def _bigquery_query():
    """GET: read sensor data, optionally filtered by date / time range.

    Auth: header `X-Shared-Secret` OR query string `?passwd=<hash>`.

    Query parameters (all optional):
      start_date  YYYY-MM-DD     inclusive lower bound on `date`
      end_date    YYYY-MM-DD     inclusive upper bound on `date`
      start_time  HH:MM:SS       inclusive lower bound on `time` (applied per row)
      end_time    HH:MM:SS       inclusive upper bound on `time` (applied per row)
      limit       int (1..1000)  max rows to return, default 50

    With no params, returns the most recent `limit` rows (default 50, newest first).
    """
    passwd = request.headers.get("X-Shared-Secret") or request.args.get("passwd")
    if passwd != PASSWORD_HASH:
        return {"status": "failed", "error": "Incorrect Password!"}, 401

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    start_time = request.args.get("start_time")
    end_time = request.args.get("end_time")

    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        return {"status": "failed", "error": "limit must be an integer"}, 400
    limit = max(1, min(limit, 1000))

    where_parts = []
    params = []
    if start_date:
        where_parts.append("date >= @start_date")
        params.append(bigquery.ScalarQueryParameter("start_date", "STRING", start_date))
    if end_date:
        where_parts.append("date <= @end_date")
        params.append(bigquery.ScalarQueryParameter("end_date", "STRING", end_date))
    if start_time:
        where_parts.append("time >= @start_time")
        params.append(bigquery.ScalarQueryParameter("start_time", "STRING", start_time))
    if end_time:
        where_parts.append("time <= @end_time")
        params.append(bigquery.ScalarQueryParameter("end_time", "STRING", end_time))

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    sql = f"""
        SELECT *
        FROM `{WEATHER_TABLE_PATH}`
        {where_sql}
        ORDER BY date DESC, time DESC
        LIMIT @limit
    """
    params.append(bigquery.ScalarQueryParameter("limit", "INT64", limit))

    try:
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=params),
        )
        rows = [dict(r.items()) for r in job.result()]
    except Exception as e:
        log.error("BigQuery query failed: %s", e)
        return {"status": "failed", "error": f"{type(e).__name__}: {e}"}, 500

    body = {
        "status": "success",
        "count": len(rows),
        "filter": {
            "start_date": start_date,
            "end_date": end_date,
            "start_time": start_time,
            "end_time": end_time,
            "limit": limit,
        },
        "rows": rows,
    }
    # Pretty-print: indent + sort_keys for stable output. default=str handles
    # any datetime/Decimal that might leak from the BigQuery row dicts.
    return Response(
        json.dumps(body, indent=2, sort_keys=True, default=str, ensure_ascii=False) + "\n",
        mimetype="application/json",
    )


@app.route("/get_outdoor_weather", methods=["POST"])
def get_outdoor_weather():
    payload = request.get_json(force=True)
    auth_err = _require_auth(payload)
    if auth_err:
        return auth_err
    city = payload.get("city")
    if not city:
        return {"status": "failed", "error": "Missing 'city'"}, 400
    data = openweather.fetch_current(city)
    if data is None:
        return {"status": "failed", "error": f"City not found: {city}"}, 404
    return {"status": "success", "city": city, "data": data}


@app.route("/get_forecast", methods=["POST"])
def get_forecast():
    payload = request.get_json(force=True)
    auth_err = _require_auth(payload)
    if auth_err:
        return auth_err
    city = payload.get("city")
    if not city:
        return {"status": "failed", "error": "Missing 'city'"}, 400
    data = openweather.fetch_forecast(city)
    if data is None:
        return {"status": "failed", "error": f"City not found: {city}"}, 404
    return {"status": "success", "city": city, "data": data}


@app.route("/voice-assistant", methods=["POST"])
def voice_assistant():
    if request.headers.get("X-Shared-Secret") != PASSWORD_HASH:
        return {"status": "failed", "error": "auth"}, 401

    wav_bytes = request.get_data()
    if not wav_bytes:
        log.warning("Received empty body on /voice-assistant")
        return {"status": "failed", "error": "empty body"}, 400

    log.info("Voice request: %d bytes of audio received", len(wav_bytes))
    transcript, language = voice.transcribe(wav_bytes)

    if not transcript:
        # No speech detected — skip intent + dispatch, return a localized
        # "didn't catch that" via the formatter's fallback table.
        intent = {"action": "unknown"}
        facts = {"status": "no_speech"}
        reply_text = voice._fallback_message({"status": "unknown_intent"}, language)
    else:
        intent = voice.parse_intent(transcript)
        facts = actions.dispatch(intent)
        reply_text = voice.format_response(facts, language, transcript)

    log.info("Synthesising reply [%s]: %r", language, reply_text)
    pcm = voice.synthesize(reply_text, language_code=language)
    log.info("TTS produced %d bytes of PCM audio", len(pcm))
    return Response(
        pcm,
        mimetype="audio/L16; rate=16000",
        headers={
            "X-Response-Text": voice.header_safe(reply_text)[:512],
            "X-Transcript": voice.header_safe(transcript)[:512],
            "X-Language": voice.header_safe(language)[:32],
            "X-Intent-Action": voice.header_safe(str(intent.get("action", "unknown")))[:64],
        },
    )


@app.route("/voice-assistant/text", methods=["POST"])
def voice_assistant_text():
    """Test helper: skip STT/TTS, send a text question, get a JSON reply.

    Same auth as /voice-assistant. Body:
        {"text": "...", "language": "fr-FR"}   # language is optional, default en-US
    """
    if request.headers.get("X-Shared-Secret") != PASSWORD_HASH:
        return {"status": "failed", "error": "auth"}, 401
    payload = request.get_json(force=True) or {}
    transcript = (payload.get("text") or "").strip()
    language = (payload.get("language") or "en-US").strip()
    if not transcript:
        return {"status": "failed", "error": "missing 'text'"}, 400
    intent = voice.parse_intent(transcript)
    facts = actions.dispatch(intent)
    reply = voice.format_response(facts, language, transcript)
    out = {
        "status": "success",
        "transcript": transcript,
        "language": language,
        "intent": intent,
        "facts": facts,
        "reply": reply,
    }
    if intent.get("_error"):
        out["intent_error"] = intent["_error"]
    return out


@app.route("/voice-assistant/models", methods=["GET"])
def voice_assistant_models():
    """Debug: list Gemini models the configured API key can see."""
    if request.headers.get("X-Shared-Secret") != PASSWORD_HASH:
        return {"status": "failed", "error": "auth"}, 401
    try:
        return {"status": "success", "models": voice.list_gemini_models()}
    except Exception as e:
        return {"status": "failed", "error": f"{type(e).__name__}: {e}"}, 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)

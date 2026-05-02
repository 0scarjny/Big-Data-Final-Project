import os

import requests
from flask import Flask, Response, request

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
        # 1. Parse payload and authenticate
        payload = request.get_json(force=True)
        auth_err = _require_auth(payload)
        if auth_err:
            return auth_err

        # 2. Extract local IoT data and location context
        local_values = payload.get("values", {})
        location = payload.get("location", "Lausanne")  # Default city if not provided

        try:
            # 3. Fetch Outside Weather (Orchestration)
            owm_data = openweather.fetch_current(location)
            if owm_data is None:
                return {"status": "failed", "error": f"City not found: {location}"}, 404

            # 4. Merge Data
            row_to_insert = {
                **local_values,
                "outdoor_temp": owm_data["main"]["temp"],
                "outdoor_humidity": owm_data["main"]["humidity"],
                "outdoor_weather": owm_data["weather"][0]["description"],
                "location": location,
            }

            # 5. Safe BigQuery Insertion
            errors = client.insert_rows_json(WEATHER_TABLE_PATH, [row_to_insert])

            if errors:
                return {"status": "failed", "error": f"BigQuery Insert Error: {errors}"}, 500

            return {"status": "success", "inserted_data": row_to_insert}, 200

        except requests.exceptions.RequestException as e:
            return {"status": "failed", "error": f"External API Error: {str(e)}"}, 502
        except Exception as e:
            return {"status": "failed", "error": str(e)}, 500


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

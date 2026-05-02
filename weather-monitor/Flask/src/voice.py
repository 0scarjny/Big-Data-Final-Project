import json
import os
import re

from google import genai
from google.genai import types as genai_types
from google.cloud import speech, texttospeech

try:
    from src.clients import (
        PROJECT_ID,
        VERTEX_LOCATION,
        get_vertex_credentials,
        speech_client,
        tts_client,
    )
    from src.logger import get_logger
except ImportError:
    from clients import (
        PROJECT_ID,
        VERTEX_LOCATION,
        get_vertex_credentials,
        speech_client,
        tts_client,
    )
    from logger import get_logger

log = get_logger("voice_assistant.voice")


SAMPLE_RATE = 16000
LANGUAGE_CODE = "en-US"
TTS_VOICE_NAME = "en-US-Standard-C"

INTENT_SYSTEM_PROMPT = """You convert a user's spoken question about home weather/sensor data into a JSON action.

Available actions:

1. historical_indoor — average reading on a past day.
   Fields: metric ("indoor_temp"|"indoor_humidity"|"indoor_co2"), day_offset (negative int, -1 = yesterday).

2. threshold_check — did a metric cross a threshold on a past day?
   Fields: metric, threshold (number), comparator ("above"|"below"), day_offset (negative int).

3. current_indoor — latest indoor reading.
   Fields: metric.

4. forecast_umbrella — will it rain in the next N hours? Use this for any rain/umbrella/outdoor question.
   Fields: hours_ahead (positive int, default 24).

5. unknown — question doesn't match any action above.
   Fields: none.

Always respond with a single JSON object, no prose, no markdown. Examples:

"What was the temperature yesterday?" -> {"action":"historical_indoor","metric":"indoor_temp","day_offset":-1}
"Did humidity exceed 50% two days ago?" -> {"action":"threshold_check","metric":"indoor_humidity","threshold":50,"comparator":"above","day_offset":-2}
"How much CO2 is there right now?" -> {"action":"current_indoor","metric":"indoor_co2"}
"Should I take an umbrella tomorrow?" -> {"action":"forecast_umbrella","hours_ahead":24}
"What's the meaning of life?" -> {"action":"unknown"}
"""


# Tried in order. Allow override via GEMINI_MODEL env var.
_GEMINI_MODEL_CANDIDATES = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash-001",
]

_genai_client = None
_chosen_model = None


def _client():
    """Returns a Vertex-AI-mode genai client. Authenticates via the service
    account loaded by clients.py (or ADC on Cloud Run / local gcloud)."""
    global _genai_client
    if _genai_client is None:
        credentials, project = get_vertex_credentials()
        kwargs = dict(vertexai=True, project=project, location=VERTEX_LOCATION)
        if credentials is not None:
            kwargs["credentials"] = credentials
        _genai_client = genai.Client(**kwargs)
        log.info("Vertex AI client ready — project=%s location=%s", project, VERTEX_LOCATION)
    return _genai_client


def list_gemini_models():
    """Returns [{'name': ..., 'methods': [...]}] for debugging."""
    out = []
    try:
        models_iter = _client().models.list()
    except Exception as e:
        log.error("models.list failed: %s", e)
        return []
    for m in models_iter:
        methods = (
            getattr(m, "supported_actions", None)
            or getattr(m, "supported_generation_methods", None)
            or []
        )
        name = (m.name or "").split("/", 1)[-1]
        out.append({"name": name, "methods": list(methods)})
    return out


def _try_one(name):
    """Probe a single model with a tiny generateContent call. Raises on failure."""
    _client().models.generate_content(
        model=name,
        contents="ping",
        config=genai_types.GenerateContentConfig(
            system_instruction=INTENT_SYSTEM_PROMPT,
            max_output_tokens=1,
        ),
    )
    return name


def _pick_model():
    global _chosen_model
    if _chosen_model is not None:
        return _chosen_model

    forced = os.environ.get("GEMINI_MODEL")
    candidates = [forced] if forced else list(_GEMINI_MODEL_CANDIDATES)

    tried = []
    for name in candidates:
        try:
            _try_one(name)
            log.info("Using Gemini model: %s", name)
            _chosen_model = name
            return _chosen_model
        except Exception as e:
            log.debug("Model %s failed probe: %s: %s", name, type(e).__name__, e)
            tried.append(f"{name}: {type(e).__name__}: {e}")

    try:
        available = list_gemini_models()
        usable = [
            m["name"] for m in available
            if any("generate" in str(x).lower() and "content" in str(x).lower() for x in m["methods"])
        ] or [m["name"] for m in available]
        log.error("No candidate model worked. Available models with generateContent: %s", usable)
    except Exception as e:
        log.error("list_models also failed: %s", e)
        usable = []

    raise RuntimeError(
        f"No usable Gemini model. Tried: {tried}. "
        f"Available: {usable}. Set GEMINI_MODEL env var."
    )


def _strip_json_fences(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def transcribe(audio_bytes):
    """Accepts WAV (LINEAR16/16kHz/mono). Returns the transcript or empty string."""
    log.debug("STT: received %d bytes of audio", len(audio_bytes))
    audio = speech.RecognitionAudio(content=audio_bytes)
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=SAMPLE_RATE,
        language_code=LANGUAGE_CODE,
        enable_automatic_punctuation=True,
    )
    response = speech_client.recognize(config=config, audio=audio)
    log.debug("STT: got %d result(s) from Google Speech", len(response.results))
    for i, result in enumerate(response.results):
        for j, alt in enumerate(result.alternatives):
            log.debug(
                "STT result[%d] alt[%d]: confidence=%.2f transcript=%r",
                i, j, alt.confidence, alt.transcript,
            )

    parts = [r.alternatives[0].transcript for r in response.results if r.alternatives]
    transcript = " ".join(parts).strip()

    if transcript:
        log.info("STT transcript: %r", transcript)
    else:
        log.warning(
            "STT returned empty transcript — %d result(s) received. "
            "Check WAV format (must be LINEAR16, 16 kHz, mono) and that the recording contains speech.",
            len(response.results),
        )
    return transcript


def parse_intent(transcript):
    if not transcript:
        return {"action": "unknown"}
    try:
        model_name = _pick_model()
        resp = _client().models.generate_content(
            model=model_name,
            contents=transcript,
            config=genai_types.GenerateContentConfig(
                system_instruction=INTENT_SYSTEM_PROMPT,
                response_mime_type="application/json",
            ),
        )
        raw = _strip_json_fences(resp.text or "")
        if not raw:
            log.warning("Gemini returned empty response for transcript: %r", transcript)
            return {"action": "unknown", "_error": "empty_response"}
        try:
            intent = json.loads(raw)
            log.info("Intent parsed: %s", intent)
            return intent
        except json.JSONDecodeError as je:
            log.error("Gemini returned non-JSON: %r — error: %s", raw[:200], je)
            return {"action": "unknown", "_error": f"bad_json: {je}", "_raw": raw[:200]}
    except Exception as e:
        log.error("Intent parse failed: %s: %s", type(e).__name__, e)
        return {"action": "unknown", "_error": f"{type(e).__name__}: {e}"}


_HEADER_SAFE = re.compile(r"[^\x20-\x7E]")


def synthesize(text):
    """Returns raw LINEAR16 PCM bytes at 16kHz. No WAV header."""
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code=LANGUAGE_CODE,
        name=TTS_VOICE_NAME,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=SAMPLE_RATE,
    )
    response = tts_client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )
    pcm = response.audio_content
    # google-cloud-texttospeech wraps LINEAR16 in a WAV container; strip the 44-byte header
    # so the device can play with Speaker.playRaw without parsing.
    if pcm[:4] == b"RIFF":
        pcm = _strip_wav_header(pcm)
    return pcm


def _strip_wav_header(wav):
    idx = wav.find(b"data")
    if idx == -1:
        return wav
    return wav[idx + 8:]


def header_safe(text):
    """ASCII-only, no control chars, for HTTP header values."""
    return _HEADER_SAFE.sub("?", text)

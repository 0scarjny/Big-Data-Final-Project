import json
import os
import re
import unicodedata

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

# STT: primary + alternates. Google's recognizer auto-picks the best match
# and reports the chosen one in result.language_code.
PRIMARY_LANGUAGE = "en-US"
ALTERNATIVE_LANGUAGES = ["fr-FR"]

# TTS voice per detected language. Keep the keys lowercase BCP-47 prefixes
# (just the language part) so we match regardless of region variants.
TTS_VOICES = {
    "en": ("en-US", "en-US-Standard-C"),
    "fr": ("fr-FR", "fr-FR-Standard-C"),
}
DEFAULT_VOICE = TTS_VOICES["en"]

# Human-readable names for the formatter prompt.
LANGUAGE_NAMES = {
    "en": "English",
    "fr": "French",
}


INTENT_SYSTEM_PROMPT = """You convert a user's spoken question about home weather/sensor data into a JSON action.

Available actions:

1. historical_indoor — average reading on a past day.
   Fields: metric ("indoor_temp"|"indoor_humidity"|"indoor_co2"), day_offset (negative int, -1 = yesterday).

2. threshold_check — did a metric cross a threshold on a past day?
   Fields: metric, threshold (number), comparator ("above"|"below"), day_offset (negative int).

3. current_indoor — latest indoor reading.
   Fields: metric.

4. forecast_weather — outdoor weather forecast for the next N hours.
   Use this for ANY question about future weather: rain, temperature, conditions,
   umbrella, going out, what to wear, etc. The action returns temperature range,
   dominant condition, rain timing, humidity — the formatter will pick whichever
   fields answer the question.
   Fields:
     - hours_ahead (positive int):
         "tomorrow"  -> 24
         "today" / "this afternoon" / "tonight" -> 12
         "this week" / "next few days" -> 72
         default if unspecified -> 24
     - city (string, OPTIONAL): include ONLY if the user explicitly mentions
       a city, town, or place name in the question (e.g. "in Paris",
       "à Genève", "in New York"). Otherwise OMIT this field — the system
       will fall back to the device's current location.
       Use the place name as the user said it, in any language; the geocoder
       handles localised spellings (Genève, Munich/München, etc.).

5. unknown — question doesn't match any action above.
   Fields: none.

The user may ask in English or French. The available actions are the same regardless of language — interpret the meaning of the question, not its surface form.

Always respond with a single JSON object, no prose, no markdown. Examples:

"What was the temperature yesterday?" -> {"action":"historical_indoor","metric":"indoor_temp","day_offset":-1}
"Quelle était la température hier?" -> {"action":"historical_indoor","metric":"indoor_temp","day_offset":-1}
"Did humidity exceed 50% two days ago?" -> {"action":"threshold_check","metric":"indoor_humidity","threshold":50,"comparator":"above","day_offset":-2}
"L'humidité a-t-elle dépassé 50% il y a deux jours?" -> {"action":"threshold_check","metric":"indoor_humidity","threshold":50,"comparator":"above","day_offset":-2}
"How much CO2 is there right now?" -> {"action":"current_indoor","metric":"indoor_co2"}
"Should I take an umbrella tomorrow?" -> {"action":"forecast_weather","hours_ahead":24}
"Faut-il prendre un parapluie demain?" -> {"action":"forecast_weather","hours_ahead":24}
"What will the weather be like tomorrow?" -> {"action":"forecast_weather","hours_ahead":24}
"Quelle sera la météo de demain?" -> {"action":"forecast_weather","hours_ahead":24}
"Va-t-il faire chaud cet après-midi?" -> {"action":"forecast_weather","hours_ahead":12}
"Will it rain in Geneva tomorrow?" -> {"action":"forecast_weather","hours_ahead":24,"city":"Geneva"}
"Est-ce qu'il va pleuvoir demain à Genève?" -> {"action":"forecast_weather","hours_ahead":24,"city":"Genève"}
"Quelle est la météo à Paris ce week-end?" -> {"action":"forecast_weather","hours_ahead":72,"city":"Paris"}
"What's the weather in New York this evening?" -> {"action":"forecast_weather","hours_ahead":12,"city":"New York"}
"What's the meaning of life?" -> {"action":"unknown"}
"""


RESPONSE_SYSTEM_PROMPT = """You are a friendly home weather assistant on a small smart-display device.

You receive: (1) the user's original spoken question, (2) the target language to reply in, (3) a JSON fact bundle from the backend.

Your job: produce ONE short natural sentence (≤ 50 words, ideally 12–20) in the target language that DIRECTLY answers what the user asked.

How to think:
1. Re-read the user's question. What did they actually ask for?
   - "What's the weather tomorrow?" → they want a general forecast (temp + conditions), NOT just whether it rains.
   - "Will it rain tomorrow?" / "Do I need an umbrella?" → focus on rain.
   - "How hot tomorrow?" / "Quelle température demain?" → focus on temperature.
   - "Was it humid yesterday?" → focus on humidity, not temperature.
2. Pick the fact-bundle fields that match the question. Ignore irrelevant ones.
3. Phrase the answer naturally in the target language.

Rules:
- Reply ONLY in the target language. No translation prefix, no quotes, no markdown.
- Reply must clearly answer what was asked. If facts.dominant_condition is "Clouds" and the user asked about the weather, mention it's cloudy — don't pivot to whether it rains.
- Use the units in the fact bundle exactly. French uses comma as decimal separator (23,6 not 23.6).
- Don't restate every field — keep the sentence short.
- If status == "no_data": apologise briefly that the data isn't available.
- If status == "bad_input": briefly explain what's wrong (unknown metric / future date / unknown city / missing info).
- If status == "error": say something went wrong and to try again.
- If status == "unknown_intent": say you didn't understand the question.
- For threshold_check: if crossed, confirm yes with the extreme value; if not, confirm no with it.
- For forecast_weather:
    * If the user asked about the WEATHER in general → mention temp range AND dominant_condition; mention rain only if rain_expected=true and relevant.
    * If the user asked specifically about RAIN/UMBRELLA → focus on rain_expected and first_rain.
    * If the user asked about TEMPERATURE → focus on temp_min/temp_max.
- Never invent numbers or facts that aren't in the bundle.

Output ONLY the sentence. Nothing else."""


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
    """Accepts WAV (LINEAR16/16kHz/mono). Returns (transcript, language_code).

    Uses Google STT's multi-language detection: the primary language is en-US
    and we list fr-FR as an alternative. The recognizer auto-picks per result
    and reports the chosen one in result.language_code.
    See https://docs.cloud.google.com/speech-to-text/docs/multiple-languages
    """
    log.debug("STT: received %d bytes of audio", len(audio_bytes))
    audio = speech.RecognitionAudio(content=audio_bytes)
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=SAMPLE_RATE,
        language_code=PRIMARY_LANGUAGE,
        alternative_language_codes=ALTERNATIVE_LANGUAGES,
        enable_automatic_punctuation=True,
    )
    response = speech_client.recognize(config=config, audio=audio)
    log.debug("STT: got %d result(s) from Google Speech", len(response.results))
    for i, result in enumerate(response.results):
        lang = getattr(result, "language_code", "") or "?"
        for j, alt in enumerate(result.alternatives):
            log.debug(
                "STT result[%d] alt[%d] lang=%s confidence=%.2f transcript=%r",
                i, j, lang, alt.confidence, alt.transcript,
            )

    parts = [r.alternatives[0].transcript for r in response.results if r.alternatives]
    transcript = " ".join(parts).strip()

    # Pick the language from the first result that has one. Fallback to primary.
    detected = PRIMARY_LANGUAGE
    for r in response.results:
        code = getattr(r, "language_code", "") or ""
        if code:
            detected = code
            break

    if transcript:
        log.info("STT transcript [%s]: %r", detected, transcript)
    else:
        log.warning(
            "STT returned empty transcript — %d result(s) received. "
            "Check WAV format (must be LINEAR16, 16 kHz, mono) and that the recording contains speech.",
            len(response.results),
        )
    return transcript, detected


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


def _voice_for_language(language_code):
    """Map a BCP-47 language code (e.g. 'fr-FR') to a (lang, voice_name) pair."""
    prefix = (language_code or "").split("-", 1)[0].lower()
    return TTS_VOICES.get(prefix, DEFAULT_VOICE)


def _language_name(language_code):
    prefix = (language_code or "").split("-", 1)[0].lower()
    return LANGUAGE_NAMES.get(prefix, "English")


def format_response(facts, language_code, transcript=""):
    """Generate a natural-language reply directly in `language_code` from
    structured facts. One LLM call, no translation step.

    Returns a plain string ready for TTS. On failure falls back to a minimal
    canned message in the target language so the user still hears something.
    """
    target = _language_name(language_code)
    user_msg = (
        f"User question: {transcript or '(no transcript)'}\n"
        f"Reply language: {target}\n"
        f"Facts:\n{json.dumps(facts, ensure_ascii=False, default=str)}"
    )
    log.debug("Formatter prompt: %s", user_msg)

    try:
        model_name = _pick_model()
        resp = _client().models.generate_content(
            model=model_name,
            contents=user_msg,
            config=_short_reply_config(),
        )
        reply = (resp.text or "").strip().strip('"').strip("'")
        if reply:
            log.info("Formatted reply [%s]: %r", language_code, reply)
            return reply
        log.warning("Formatter returned empty text for facts: %s — finish_reason=%s",
                    facts, _finish_reason(resp))
    except Exception as e:
        log.error("Formatter failed: %s: %s", type(e).__name__, e)

    return _fallback_message(facts, language_code)


def _short_reply_config():
    """GenerateContentConfig tuned for short, deterministic responses.

    Gemini 2.5 models burn 'thinking' tokens against max_output_tokens, so
    a budget like 200 leaves almost nothing for the visible reply. We disable
    thinking when supported (thinking_budget=0) and bump max_output_tokens.
    Falls back gracefully on older SDKs that don't expose ThinkingConfig.
    """
    base = dict(
        system_instruction=RESPONSE_SYSTEM_PROMPT,
        max_output_tokens=512,
        temperature=0.3,
    )
    ThinkingConfig = getattr(genai_types, "ThinkingConfig", None)
    if ThinkingConfig is not None:
        try:
            base["thinking_config"] = ThinkingConfig(thinking_budget=0)
        except Exception as e:
            log.debug("ThinkingConfig(thinking_budget=0) unsupported: %s", e)
    return genai_types.GenerateContentConfig(**base)


def _finish_reason(resp):
    """Pull a human-readable finish reason out of a generate_content response."""
    try:
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            return str(getattr(candidates[0], "finish_reason", "unknown"))
    except Exception:
        pass
    return "unknown"


_FALLBACK = {
    "en": {
        "ok":            "I have your data but couldn't phrase a reply.",
        "no_data":       "I don't have data for that period.",
        "bad_input":     "I couldn't process that request.",
        "error":         "Something went wrong. Please try again.",
        "unknown_intent":"Sorry, I didn't understand the question.",
        "default":       "Sorry, something went wrong.",
    },
    "fr": {
        "ok":            "J'ai les données mais je n'ai pas pu formuler de réponse.",
        "no_data":       "Je n'ai pas de données pour cette période.",
        "bad_input":     "Je n'ai pas pu traiter cette demande.",
        "error":         "Une erreur s'est produite. Veuillez réessayer.",
        "unknown_intent":"Désolé, je n'ai pas compris la question.",
        "default":       "Désolé, une erreur s'est produite.",
    },
}


def _fallback_message(facts, language_code):
    prefix = (language_code or "").split("-", 1)[0].lower()
    table = _FALLBACK.get(prefix, _FALLBACK["en"])
    status = (facts or {}).get("status", "default")
    return table.get(status, table["default"])


def synthesize(text, language_code=PRIMARY_LANGUAGE):
    """Returns raw LINEAR16 PCM bytes at 16kHz. No WAV header.

    The voice is picked based on `language_code` (e.g. 'fr-FR' uses a French voice)."""
    lang, voice_name = _voice_for_language(language_code)
    log.debug("TTS: synthesising %d chars with voice %s (%s)", len(text), voice_name, lang)
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code=lang,
        name=voice_name,
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
    """ASCII-only, no control chars, for HTTP header values.

    Strips accents instead of replacing with '?', so French text stays
    readable when shown on the device's status label (e.g. 'élève' -> 'eleve').
    """
    if not text:
        return ""
    # NFKD splits 'é' -> 'e' + combining acute; the combining mark is non-ASCII
    # and gets dropped by encode/decode.
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _HEADER_SAFE.sub("?", normalized)

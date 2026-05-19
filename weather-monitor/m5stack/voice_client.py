# Voice-assistant module for the M5Stack app.
#
# Records audio from the mic, uploads the WAV to the Flask /voice-assistant
# endpoint, and plays back the synthesized PCM reply via the speaker.
# UI updates (status, reply text, spinner) are pushed through callbacks
# registered by ui.py.

import _thread
import time
import json

from M5 import Mic, Speaker
import requests2

from config import SHARED_SECRET, VOICE_URL, ANNOUNCEMENT_URL

# --- Audio config ---
SAMPLE_RATE = 16000
MAX_RECORD_TIME_SEC = 10
BYTES_PER_SAMPLE = 2

# Cloud Run + Gemini intent parsing + TTS can legitimately take 60+ seconds.
# Don't time out faster than that or you'll cancel valid requests.
HTTP_TIMEOUT_S = 90

# Hard upper bound on how long playback may block before we give up on the
# Speaker. Without this, a stuck Speaker.isPlaying() flag traps the worker.
MAX_PLAYBACK_S = 15

# Simple safety net: if the worker thread is still marked busy this long
# after it was spawned, something is genuinely wedged — force-reset so the
# UI isn't stuck. Set well above HTTP_TIMEOUT_S + MAX_PLAYBACK_S so it never
# fires for normal slow-but-successful requests.
VOICE_WATCHDOG_MS = 120_000

# --- Recording state ---
_rec_data = None
_recorded_bytes = 0
_start_time = 0
_recording = False
_busy = False  # True from press until reply finished playing
_thread_started_ms = 0  # ticks_ms() when the worker thread was spawned

# --- Device location (set by main.py once IP geolocation completes) ---
# Sent as X-Device-Location with each request so the backend has a fallback
# city for "what's the weather tomorrow?" type questions where the user
# didn't name a place explicitly.
_device_location = None

# --- UI callbacks (set by ui.register_callbacks) ---
_on_status = None
_on_reply = None
_on_spinner = None


def set_location(location):
    """Called by main.py once the device's IP-based location is known."""
    global _device_location
    _device_location = location
    print("[voice] device location set to:", location)


def prepare():
    """Allocate the recording buffer once. Safe to call multiple times."""
    global _rec_data
    if _rec_data is None:
        _rec_data = bytearray(SAMPLE_RATE * BYTES_PER_SAMPLE * MAX_RECORD_TIME_SEC)


def register_callbacks(on_status, on_reply, on_spinner):
    """ui.py wires its label/spinner setters here so this module stays UI-agnostic."""
    global _on_status, _on_reply, _on_spinner
    _on_status = on_status
    _on_reply = on_reply
    _on_spinner = on_spinner


def _status(text):
    if _on_status:
        try:
            _on_status(text)
        except Exception as e:
            print("[voice] status cb error:", e)


def _reply(text):
    if _on_reply:
        try:
            _on_reply(text)
        except Exception as e:
            print("[voice] reply cb error:", e)


def _spinner(visible):
    if _on_spinner:
        try:
            _on_spinner(visible)
        except Exception as e:
            print("[voice] spinner cb error:", e)


def is_busy():
    """True while a recording or backend request is in flight. Used to block
    button-press re-entry and to defer the BigQuery upload one cycle."""
    return _busy or _recording


def watchdog_check():
    """Safety net: if the worker thread has been busy for more than
    VOICE_WATCHDOG_MS, something is genuinely wedged — force-reset so the UI
    can accept new input. MicroPython _thread has no cancel, so any orphan
    keeps running until its socket fails. Polled by main.network_task."""
    global _busy
    if not _busy:
        return
    elapsed = time.ticks_diff(time.ticks_ms(), _thread_started_ms)
    if elapsed > VOICE_WATCHDOG_MS:
        print("[voice] watchdog: busy for {} ms, force-resetting".format(elapsed))
        _busy = False
        _spinner(False)
        _status("Timed out")


def start_recording():
    """Call on PRESSED. Begins capturing audio; returns immediately."""
    global _start_time, _recording
    if _busy or _recording:
        return False
    prepare()
    _status("Recording...")
    Speaker.end()  # mic + speaker share I2S on Core S3
    Mic.begin()
    _start_time = time.ticks_ms()
    Mic.record(_rec_data, SAMPLE_RATE, False)
    _recording = True
    return True


def stop_and_send():
    """Call on RELEASED. Finalises the recording and spawns a worker thread
    that builds the WAV in RAM, uploads it, and plays the reply."""
    global _recorded_bytes, _recording, _busy, _thread_started_ms
    if not _recording:
        return False
    Mic.end()
    _recording = False

    elapsed_ms = time.ticks_diff(time.ticks_ms(), _start_time)
    _recorded_bytes = int((elapsed_ms / 1000) * SAMPLE_RATE * BYTES_PER_SAMPLE)
    if _recorded_bytes > len(_rec_data):
        _recorded_bytes = len(_rec_data)
    if _recorded_bytes < SAMPLE_RATE * BYTES_PER_SAMPLE // 4:  # < 0.25s
        _status("Too short, try again")
        return False

    # Audio stays in _rec_data (RAM); the WAV header is built at upload time.
    # Skipping the /flash/recording.wav round-trip removes ~100–300 ms of flash
    # I/O latency and avoids unnecessary flash wear.
    _busy = True
    _spinner(True)
    _thread_started_ms = time.ticks_ms()
    _thread.start_new_thread(_ask_backend_thread, ())
    return True


def _get_header(headers, name):
    """Read an HTTP header robustly across requests2 versions.

    Different MicroPython HTTP libs expose .headers as: a regular dict with
    original-case keys, a dict with lowercased keys, a list of (key, value)
    tuples, or not at all. Try each shape until something matches case-
    insensitively. Returns "" when missing.
    """
    if not headers:
        return ""
    target = name.lower()

    # 1. dict-like .get() with several casings. Avoid str.title() — it's not
    # implemented in MicroPython.
    if hasattr(headers, "get"):
        for k in (name, target, name.upper()):
            try:
                v = headers.get(k)
                if v:
                    return v
            except Exception:
                pass

    # 2. dict.items() — iterate and compare case-insensitively.
    try:
        for k, v in headers.items():
            if str(k).lower() == target:
                return v
    except Exception:
        pass

    # 3. iterable of (key, value) tuples.
    try:
        for entry in headers:
            if isinstance(entry, tuple) and len(entry) == 2:
                k, v = entry
                if str(k).lower() == target:
                    return v
    except Exception:
        pass

    return ""


def _wav_header(data_size, sample_rate=SAMPLE_RATE, num_channels=1, bits_per_sample=16):
    """Build the 44-byte canonical RIFF/WAVE PCM header in memory.

    Mirrors the layout previously written to /flash/recording.wav, just
    without the flash round-trip — the audio is already in RAM in _rec_data,
    so we prepend this header at upload time and post header + audio directly.
    """
    byte_rate = sample_rate * num_channels * (bits_per_sample // 8)
    block_align = num_channels * (bits_per_sample // 8)
    h = bytearray(44)
    h[0:4]   = b'RIFF'
    h[4:8]   = (36 + data_size).to_bytes(4, 'little')
    h[8:12]  = b'WAVE'
    h[12:16] = b'fmt '
    h[16:20] = (16).to_bytes(4, 'little')           # fmt chunk size
    h[20:22] = (1).to_bytes(2, 'little')            # PCM format tag
    h[22:24] = (num_channels).to_bytes(2, 'little')
    h[24:28] = (sample_rate).to_bytes(4, 'little')
    h[28:32] = (byte_rate).to_bytes(4, 'little')
    h[32:34] = (block_align).to_bytes(2, 'little')
    h[34:36] = (bits_per_sample).to_bytes(2, 'little')
    h[36:40] = b'data'
    h[40:44] = (data_size).to_bytes(4, 'little')
    return h


def _play_pcm(pcm):
    """Play raw 16 kHz / 16-bit / mono PCM through the speaker, capped by
    MAX_PLAYBACK_S so a stuck Speaker.isPlaying() flag can't trap the worker.
    Mic and Speaker share I2S, so we own Speaker.begin/end here."""
    Speaker.begin()
    Speaker.setVolumePercentage(100)
    print("[voice] playRaw start")
    t_play = time.ticks_ms()
    Speaker.playRaw(memoryview(pcm), SAMPLE_RATE)
    print("[voice] playRaw returned in {}ms".format(
        time.ticks_diff(time.ticks_ms(), t_play)))
    playback_deadline = time.ticks_add(time.ticks_ms(), MAX_PLAYBACK_S * 1000)
    while Speaker.isPlaying():
        if time.ticks_diff(playback_deadline, time.ticks_ms()) <= 0:
            print("[voice] playback cap hit, forcing stop")
            break
        time.sleep_ms(20)
    Speaker.end()


# Announcement requests are short JSON → small PCM. No Gemini intent step on
# the server, so the round-trip is much faster than /voice-assistant.
ANNOUNCEMENT_TIMEOUT_S = 30

_announce_args = None  # holds (location, temp, humidity, co2, context, on_done)


def request_announcement(location, indoor_temp, indoor_humidity, indoor_co2,
                         context, on_done=None):
    """Ask the backend whether anything is worth announcing right now and,
    if so, play the synthesized reply through the speaker.

    Reuses the voice-assistant `_busy` flag + watchdog so concurrent BigQuery
    uploads defer one cycle (see main.network_task) — two TLS sockets is the
    exact failure mode the existing comment in main.py warns about.

    `on_done(success: bool)` runs from the worker thread once playback
    finishes (or once the request errors out). Caller uses this to bump the
    presence rate-limit only on success.
    """
    global _busy, _thread_started_ms, _announce_args
    if _busy or _recording:
        return False
    _busy = True
    _thread_started_ms = time.ticks_ms()
    _announce_args = (location, indoor_temp, indoor_humidity, indoor_co2, context, on_done)
    _thread.start_new_thread(_announcement_thread, ())
    return True


def _announcement_thread():
    """Single-shot proactive announcement: POST sensor snapshot + context →
    play returned PCM. No retries — next presence trigger is the retry."""
    global _busy
    location, t, h, co2, context, on_done = _announce_args
    success = False
    try:
        body = {
            "location": location,
            "context": context,
            "language": "en-US",
        }
        if t is not None:
            body["indoor_temp"] = t
        if h is not None:
            body["indoor_humidity"] = h
        if co2 is not None:
            body["indoor_co2"] = co2

        headers = {
            "Content-Type": "application/json",
            "X-Shared-Secret": SHARED_SECRET,
        }

        t0 = time.ticks_ms()
        try:
            resp = requests2.post(
                ANNOUNCEMENT_URL,
                data=json.dumps(body),
                headers=headers,
                timeout=ANNOUNCEMENT_TIMEOUT_S,
            )
        except Exception as e:
            print("[announce] network error:", e)
            return
        t1 = time.ticks_ms()

        try:
            if resp.status_code == 204:
                # Server decided nothing was worth saying (e.g. morning_check
                # with no rain). Still treat as success so we honour the
                # cooldown and don't re-poll the endpoint immediately.
                print("[announce] 204 no content (silent)")
                success = True
                return

            if resp.status_code != 200:
                print("[announce] HTTP", resp.status_code)
                return

            reply_text = _get_header(getattr(resp, "headers", None), "X-Response-Text")
            t2 = time.ticks_ms()
            if reply_text:
                print("[announce] saying:", reply_text)

            # Read body in larger chunks to dodge the O(n^2) bytes concat that
            # requests2.content does internally — on 150 KB of PCM that's
            # tens of seconds of GC churn on the ESP32.
            CHUNK = 4096
            clen_str = _get_header(getattr(resp, "headers", None), "Content-Length")
            try:
                clen = int(clen_str) if clen_str else 0
            except ValueError:
                clen = 0
            raw = getattr(resp, "raw", None)
            if raw is not None and clen > 0:
                read_path = "readinto"
                pcm = bytearray(clen)
                view = memoryview(pcm)
                pos = 0
                while pos < clen:
                    n = raw.readinto(view[pos:pos + CHUNK])
                    if not n:
                        break
                    pos += n
                if pos < clen:
                    pcm = pcm[:pos]
            elif raw is not None:
                read_path = "extend"
                pcm = bytearray()
                while True:
                    chunk = raw.read(CHUNK)
                    if not chunk:
                        break
                    pcm.extend(chunk)
            else:
                read_path = "content"
                pcm = resp.content
            t3 = time.ticks_ms()
        finally:
            try:
                resp.close()
            except Exception:
                pass

        print("[announce] timings: post={}ms headers={}ms body={}ms({}) size={}B".format(
            time.ticks_diff(t1, t0),
            time.ticks_diff(t2, t1),
            time.ticks_diff(t3, t2),
            read_path,
            len(pcm),
        ))
        _play_pcm(pcm)
        success = True
    except Exception as e:
        print("[announce] thread error:", e)
    finally:
        _busy = False
        if on_done:
            try:
                on_done(success)
            except Exception as e:
                print("[announce] on_done error:", e)


def _ask_backend_thread():
    """Single-shot voice request:
        Asking... -> Speaking... -> Ready
    No retries, no heartbeats, no per-stage status churn. The HTTP timeout
    (HTTP_TIMEOUT_S) handles the network side; the watchdog handles the
    cosmic-ray case where everything else fails.
    """
    global _busy
    try:
        _status("Asking...")

        # Build the WAV payload in RAM. Held twice momentarily (~320 KB at
        # 16 kHz/16-bit/5 s) — fine on Core S3 PSRAM.
        audio_view = memoryview(_rec_data)[:_recorded_bytes]
        wav = bytes(_wav_header(len(audio_view))) + bytes(audio_view)

        headers = {
            "Content-Type": "audio/wav",
            "X-Shared-Secret": SHARED_SECRET,
        }
        if _device_location:
            headers["X-Device-Location"] = str(_device_location)

        # One attempt. requests2 will block here for up to HTTP_TIMEOUT_S
        # seconds; that's expected — Cloud Run + Gemini is slow.
        try:
            resp = requests2.post(
                VOICE_URL, data=wav, headers=headers, timeout=HTTP_TIMEOUT_S,
            )
        except Exception as e:
            print("[voice] network error:", e)
            _status("Network error")
            _reply(str(e))
            return

        if resp.status_code != 200:
            print("[voice] HTTP", resp.status_code)
            _status("Error " + str(resp.status_code))
            try:
                _reply(resp.text[:200])
            except Exception:
                _reply("")
            resp.close()
            return

        # Pull the transcript + reply text from response headers, and the
        # PCM body from .content. Header shape varies across requests2
        # versions — _get_header handles all the cases we've seen.
        headers_obj = getattr(resp, "headers", None)
        transcript = _get_header(headers_obj, "X-Transcript")
        reply_text = _get_header(headers_obj, "X-Response-Text")
        pcm = resp.content
        resp.close()

        if transcript:
            print("[voice] heard:", transcript)
        if reply_text:
            print("[voice] reply:", reply_text)
        _reply(reply_text or "(no text)")
        _spinner(False)
        _status("Speaking...")

        _play_pcm(pcm)

        _status("Ready")
    except Exception as e:
        print("[voice] thread error:", e)
        _status("Error")
        _reply(str(e))
    finally:
        _spinner(False)
        _busy = False

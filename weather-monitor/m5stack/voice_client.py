# Voice-assistant module for the M5Stack app.
#
# Records audio from the mic, uploads the WAV to the Flask /voice-assistant
# endpoint, and plays back the synthesized PCM reply via the speaker.
# UI updates (status, reply text, spinner) are pushed through callbacks
# registered by ui.py.

import _thread
import time

from M5 import Mic, Speaker
import requests2

# --- Audio config ---
SAMPLE_RATE = 16000
MAX_RECORD_TIME_SEC = 5
BYTES_PER_SAMPLE = 2

# --- Backend config ---
VOICE_URL = 'https://flask-app-868833155300.europe-west6.run.app/voice-assistant'
SHARED_SECRET = '03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4'
HTTP_TIMEOUT_S = 30

# Hard upper bound on how long playback may block before we give up on the
# Speaker. Without this, a stuck Speaker.isPlaying() flag traps the worker
# thread forever and the UI never recovers.
MAX_PLAYBACK_S = 15

# External watchdog: if _busy is still True this long after the worker thread
# was spawned, the thread is presumed wedged (HTTP socket hung past its own
# timeout, etc.). main.py polls watchdog_check() to force-reset the flag so a
# new recording can be started. > HTTP_TIMEOUT_S + MAX_PLAYBACK_S.
VOICE_WATCHDOG_MS = 45_000

# --- Recording state ---
_rec_data = None
_recorded_bytes = 0
_start_time = 0
_recording = False
_busy = False  # True from press until reply finished playing
_thread_started_ms = 0  # ticks_ms() when the current worker thread was spawned

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
    """Force-reset _busy if the worker thread has been alive longer than
    VOICE_WATCHDOG_MS. MicroPython _thread has no cancel, so the orphan thread
    keeps running until its socket fails — but the UI is no longer stuck."""
    global _busy
    if _busy and time.ticks_diff(time.ticks_ms(), _thread_started_ms) > VOICE_WATCHDOG_MS:
        print("[voice] watchdog: forcing _busy reset")
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
    """Call on RELEASED. Finalises the recording, saves the WAV, and spawns
    a worker thread to upload it and play the reply."""
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

    _status("Saving...")
    valid_audio = memoryview(_rec_data)[:_recorded_bytes]
    try:
        _save_wav("/flash/recording.wav", valid_audio, SAMPLE_RATE)
    except Exception as e:
        print("[voice] save_wav failed:", e)
        _status("Save error")
        return False

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


def _save_wav(filename, pcm_data, sample_rate, num_channels=1, bits_per_sample=16):
    with open(filename, "wb") as f:
        byte_rate = sample_rate * num_channels * (bits_per_sample // 8)
        block_align = num_channels * (bits_per_sample // 8)
        data_size = len(pcm_data)

        f.write(b'RIFF')
        f.write((36 + data_size).to_bytes(4, 'little'))
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write((16).to_bytes(4, 'little'))
        f.write((1).to_bytes(2, 'little'))  # PCM
        f.write((num_channels).to_bytes(2, 'little'))
        f.write((sample_rate).to_bytes(4, 'little'))
        f.write((byte_rate).to_bytes(4, 'little'))
        f.write((block_align).to_bytes(2, 'little'))
        f.write((bits_per_sample).to_bytes(2, 'little'))
        f.write(b'data')
        f.write((data_size).to_bytes(4, 'little'))
        f.write(pcm_data)


def _ask_backend_thread():
    global _busy
    try:
        _status("Uploading...")
        with open("/flash/recording.wav", "rb") as f:
            wav = f.read()

        headers = {
            "Content-Type": "audio/wav",
            "X-Shared-Secret": SHARED_SECRET,
        }
        if _device_location:
            headers["X-Device-Location"] = str(_device_location)
        try:
            resp = requests2.post(
                VOICE_URL,
                data=wav,
                headers=headers,
                timeout=HTTP_TIMEOUT_S,
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

        # MicroPython requests2 stores headers in various ways depending on
        # version (dict / dict-like with lowercase keys / list of tuples / not
        # at all). Try every shape we've seen.
        headers_obj = getattr(resp, "headers", None)
        print("[voice] headers type:", type(headers_obj).__name__,
              " value:", repr(headers_obj)[:200])
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
        _status("Playing...")

        Speaker.begin()
        Speaker.setVolumePercentage(100)
        Speaker.playRaw(memoryview(pcm), SAMPLE_RATE)
        playback_deadline = time.ticks_add(time.ticks_ms(), MAX_PLAYBACK_S * 1000)
        while Speaker.isPlaying():
            if time.ticks_diff(playback_deadline, time.ticks_ms()) <= 0:
                print("[voice] playback cap hit, forcing stop")
                break
            time.sleep_ms(20)
        Speaker.end()

        _status("Ready")
    except Exception as e:
        print("[voice] thread error:", e)
        _status("Error")
        _reply(str(e))
    finally:
        _spinner(False)
        _busy = False

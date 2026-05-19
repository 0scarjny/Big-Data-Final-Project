# Voice client for the M5Stack weather monitor.
#
# Two request kinds share one long-lived worker thread:
#   - 'announcement': PIR motion → proactive TTS via /critical-announcement
#   - 'voice':        button press → STT/intent/TTS via /voice-assistant
#
# Concurrency model:
#   - Single _busy flag. A second submission while busy is rejected.
#   - Single _pending slot. Submitter writes; worker reads then clears.
#   - One long-lived worker thread, started lazily at first submission and
#     wrapped in a never-die outer try/except so a stray exception can't
#     permanently brick the device.
#   - Module-level _current_resp lets the watchdog cancel a stuck request by
#     closing the response — the worker's readinto raises and unwinds cleanly.
#   - Per-read socket timeout (best-effort) bounds individual reads when the
#     underlying requests2 build exposes the socket.

import _thread
import time
import json

from M5 import Mic, Speaker
import requests2

from config import SHARED_SECRET, VOICE_URL, ANNOUNCEMENT_URL


# ===== Audio config =====
# 8 kHz mono LINEAR16 matches the Flask backend (Google STT recogniser +
# Google TTS synthesiser are both configured for 8 kHz in Flask/src/voice.py).
# Keeping the device and backend aligned avoids the 2× playback speed bug we
# had when the device played 8 kHz PCM at 16 kHz, and halves the body size
# on the announcement response (~150 KB → ~75 KB).
SAMPLE_RATE = 8000
MAX_RECORD_TIME_SEC = 10
BYTES_PER_SAMPLE = 2


# ===== Timeouts =====
# POST connect + headers. /voice-assistant runs server-side STT + Gemini
# intent + TTS and can legitimately take 60+ s; /critical-announcement is
# shorter (no STT, no intent step).
ANNOUNCEMENT_CONNECT_TIMEOUT_S = 30
VOICE_CONNECT_TIMEOUT_S = 90

# Hard deadline for the PCM body read. On this device 150 KB typically
# transfers in 25-40 s; anything past 60 s is a stalled socket and we bail.
BODY_READ_TIMEOUT_S = 60

# Per-read socket timeout, applied best-effort to the underlying socket.
# If requests2 doesn't expose it, the body deadline + watchdog still bound
# total time.
SOCKET_READ_TIMEOUT_S = 5

# Playback cap — Speaker.isPlaying() sometimes sticks True after the I2S
# buffer drains; this keeps it from trapping the worker.
MAX_PLAYBACK_S = 15

# Last-resort watchdog. After this, the watchdog closes _current_resp
# (forcing the worker's readinto to error) and clears _busy. Must exceed
# the worst sane flow: VOICE_CONNECT_TIMEOUT_S + BODY_READ_TIMEOUT_S +
# MAX_PLAYBACK_S = 165 s; rounded up for slack.
VOICE_WATCHDOG_MS = 180_000


# ===== State =====
# Recording (button-driven, sized once via prepare())
_rec_data = None
_recorded_bytes = 0
_rec_started_ms = 0
_recording = False

# Single-slot worker state
_busy = False
_busy_started_ms = 0
_pending = None     # ('announcement', args) | ('voice', args) | None

# Active HTTP response — the watchdog closes this to cancel a stuck read.
_current_resp = None

# Worker thread lifecycle
_worker_started = False

# Device-set state
_device_location = None

# UI callbacks (registered by ui.py)
_on_status = None
_on_reply = None
_on_spinner = None


# ===== UI callback shims =====

def _status(text):
    if _on_status:
        try: _on_status(text)
        except Exception as e: print("[voice] status cb error:", e)


def _reply(text):
    if _on_reply:
        try: _on_reply(text)
        except Exception as e: print("[voice] reply cb error:", e)


def _spinner(visible):
    if _on_spinner:
        try: _on_spinner(visible)
        except Exception as e: print("[voice] spinner cb error:", e)


# ===== Public API =====

def set_location(location):
    """Called by main.py once the device's IP-based location is known."""
    global _device_location
    _device_location = location
    print("[voice] device location set to:", location)


def register_callbacks(on_status, on_reply, on_spinner):
    """ui.py wires its label/spinner setters here so this module stays UI-agnostic."""
    global _on_status, _on_reply, _on_spinner
    _on_status = on_status
    _on_reply = on_reply
    _on_spinner = on_spinner


def prepare():
    """Allocate the recording buffer once. Safe to call multiple times."""
    global _rec_data
    if _rec_data is None:
        _rec_data = bytearray(SAMPLE_RATE * BYTES_PER_SAMPLE * MAX_RECORD_TIME_SEC)


def is_busy():
    """True while a recording or backend request is in flight. Used to block
    button-press re-entry and to defer the BigQuery upload one cycle."""
    return _busy or _recording


def request_announcement(location, indoor_temp, indoor_humidity, indoor_co2,
                          context, on_response_ok=None):
    """Submit a proactive announcement. Returns True if accepted, False if
    the worker is already busy.

    on_response_ok(context) runs from the worker the moment the backend
    returns 200/204 — BEFORE the body read. Callers mark the presence
    cooldown here so a second motion event during the read can't queue a
    duplicate request.
    """
    global _busy, _busy_started_ms, _pending
    if _busy or _recording:
        return False
    _busy = True
    _busy_started_ms = time.ticks_ms()
    _pending = ('announcement',
                (location, indoor_temp, indoor_humidity, indoor_co2,
                 context, on_response_ok))
    _ensure_worker()
    return True


def start_recording():
    """Call on PRESSED. Begins capturing audio; returns immediately."""
    global _rec_started_ms, _recording
    if _busy or _recording:
        return False
    prepare()
    _status("Recording...")
    Speaker.end()  # mic + speaker share I2S on Core S3
    Mic.begin()
    _rec_started_ms = time.ticks_ms()
    Mic.record(_rec_data, SAMPLE_RATE, False)
    _recording = True
    return True


def stop_and_send():
    """Call on RELEASED. Finalises the recording and submits to the worker."""
    global _recorded_bytes, _recording, _busy, _busy_started_ms, _pending
    if not _recording:
        return False
    Mic.end()
    _recording = False

    elapsed_ms = time.ticks_diff(time.ticks_ms(), _rec_started_ms)
    _recorded_bytes = int((elapsed_ms / 1000) * SAMPLE_RATE * BYTES_PER_SAMPLE)
    if _recorded_bytes > len(_rec_data):
        _recorded_bytes = len(_rec_data)
    if _recorded_bytes < SAMPLE_RATE * BYTES_PER_SAMPLE // 4:  # < 0.25s
        _status("Too short, try again")
        return False

    _busy = True
    _spinner(True)
    _busy_started_ms = time.ticks_ms()
    # Snapshot the recording so the next press can clobber _rec_data freely.
    audio_bytes = bytes(memoryview(_rec_data)[:_recorded_bytes])
    _pending = ('voice', (audio_bytes,))
    _ensure_worker()
    return True


def watchdog_check():
    """Last-resort cancellation. After VOICE_WATCHDOG_MS, close the active
    response so the worker's readinto raises and the thread unwinds cleanly,
    then clear _busy. Polled by main.network_task.

    Clearing _busy here can't cause a duplicate announcement because the
    cooldown was already marked eagerly when the 200/204 came back."""
    global _busy, _current_resp
    if not _busy:
        return
    elapsed = time.ticks_diff(time.ticks_ms(), _busy_started_ms)
    if elapsed > VOICE_WATCHDOG_MS:
        print("[voice] watchdog: cancelling after {}ms".format(elapsed))
        if _current_resp is not None:
            try:
                _current_resp.close()
            except Exception as e:
                print("[voice] watchdog close error:", e)
            _current_resp = None
        _busy = False
        _spinner(False)
        _status("Timed out")


# ===== Worker thread =====

def _ensure_worker():
    global _worker_started
    if not _worker_started:
        _worker_started = True
        _thread.start_new_thread(_worker_loop, ())


def _worker_loop():
    """Long-lived worker. Polls _pending; processes one request at a time.
    Outer try/except keeps the loop alive even when a request crashes — a
    dead worker would silently brick the device because nothing else
    monitors thread liveness."""
    global _busy, _pending
    while True:
        try:
            pending = _pending
            if pending is None:
                time.sleep_ms(100)
                continue
            kind, args = pending
            try:
                if kind == 'announcement':
                    _do_announcement(*args)
                elif kind == 'voice':
                    _do_voice(*args)
                else:
                    print("[voice] unknown request kind:", kind)
            finally:
                _pending = None
                _spinner(False)
                _busy = False
        except Exception as e:
            print("[voice] worker loop error:", e)
            _pending = None
            _busy = False
            time.sleep_ms(500)


# ===== Announcement flow =====

def _do_announcement(location, t, h, co2, context, on_response_ok):
    body = {"location": location, "context": context, "language": "en-US"}
    if t is not None: body["indoor_temp"] = t
    if h is not None: body["indoor_humidity"] = h
    if co2 is not None: body["indoor_co2"] = co2
    headers = {
        "Content-Type": "application/json",
        "X-Shared-Secret": SHARED_SECRET,
    }

    pcm = _post_and_read_pcm(
        url=ANNOUNCEMENT_URL,
        data=json.dumps(body),
        headers=headers,
        connect_timeout_s=ANNOUNCEMENT_CONNECT_TIMEOUT_S,
        body_timeout_s=BODY_READ_TIMEOUT_S,
        log_tag="announce",
        on_response=lambda status, reply_text: _on_announcement_response(
            status, reply_text, context, on_response_ok
        ),
    )
    if pcm:
        _play_pcm(pcm)


def _on_announcement_response(status_code, reply_text, context, on_response_ok):
    """Runs after headers, before body read. Mark cooldown + log reply."""
    if status_code == 204:
        print("[announce] 204 no content (silent)")
    if status_code in (200, 204) and on_response_ok:
        try:
            on_response_ok(context)
        except Exception as e:
            print("[announce] on_response_ok error:", e)
    if reply_text:
        print("[announce] saying:", reply_text)


# ===== Voice-assistant flow =====

def _do_voice(audio_bytes):
    _status("Asking...")
    wav = bytes(_wav_header(len(audio_bytes))) + audio_bytes
    headers = {
        "Content-Type": "audio/wav",
        "X-Shared-Secret": SHARED_SECRET,
    }
    if _device_location:
        headers["X-Device-Location"] = str(_device_location)

    pcm = _post_and_read_pcm(
        url=VOICE_URL,
        data=wav,
        headers=headers,
        connect_timeout_s=VOICE_CONNECT_TIMEOUT_S,
        body_timeout_s=BODY_READ_TIMEOUT_S,
        log_tag="voice",
        on_response=_on_voice_response,
    )
    if pcm:
        _status("Speaking...")
        _play_pcm(pcm)
        _status("Ready")
    else:
        _status("Error")


def _on_voice_response(status_code, reply_text):
    if status_code != 200:
        _reply("HTTP " + str(status_code))
    else:
        _reply(reply_text or "(no text)")
    _spinner(False)


# ===== HTTP helper (shared by both flows) =====

def _post_and_read_pcm(url, data, headers, connect_timeout_s, body_timeout_s,
                       log_tag, on_response):
    """POST and read a PCM response body. Returns body bytes or None on any
    failure. on_response(status_code, reply_text) runs after headers arrive
    but before the body read, regardless of status."""
    global _current_resp
    t0 = time.ticks_ms()
    resp = None
    try:
        try:
            resp = requests2.post(url, data=data, headers=headers,
                                  timeout=connect_timeout_s)
            _current_resp = resp
        except Exception as e:
            print("[{}] post error: {}".format(log_tag, e))
            return None
        t1 = time.ticks_ms()

        status = resp.status_code
        reply_text = _get_header(getattr(resp, "headers", None), "X-Response-Text")

        try:
            on_response(status, reply_text)
        except Exception as e:
            print("[{}] on_response error: {}".format(log_tag, e))

        if status == 204:
            return None
        if status != 200:
            print("[{}] HTTP {}".format(log_tag, status))
            return None

        deadline_ms = time.ticks_add(time.ticks_ms(), body_timeout_s * 1000)
        body, ok, path = _read_body_with_deadline(resp, deadline_ms)
        t2 = time.ticks_ms()

        print("[{}] timings: post={}ms body={}ms({}) size={}B".format(
            log_tag,
            time.ticks_diff(t1, t0),
            time.ticks_diff(t2, t1),
            path if ok else path + "-fail",
            len(body) if body else 0,
        ))
        return body if ok else None
    finally:
        _current_resp = None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def _read_body_with_deadline(resp, deadline_ms):
    """Read PCM body in chunks with a hard deadline + per-read socket
    timeout (best-effort). Returns (bytes, ok, path_label)."""
    raw = getattr(resp, "raw", None)
    if raw is None:
        try:
            body = resp.content
            ok = time.ticks_diff(deadline_ms, time.ticks_ms()) > 0
            return body, ok, "content"
        except Exception as e:
            print("[http] content read error:", e)
            return None, False, "content"

    _try_set_socket_timeout(raw, SOCKET_READ_TIMEOUT_S)

    clen_str = _get_header(getattr(resp, "headers", None), "Content-Length")
    try:
        clen = int(clen_str) if clen_str else 0
    except ValueError:
        clen = 0

    CHUNK = 4096
    if clen > 0:
        pcm = bytearray(clen)
        view = memoryview(pcm)
        pos = 0
        while pos < clen:
            if time.ticks_diff(deadline_ms, time.ticks_ms()) <= 0:
                print("[http] body deadline at {}/{}B".format(pos, clen))
                return None, False, "readinto"
            try:
                n = raw.readinto(view[pos:pos + CHUNK])
            except Exception as e:
                print("[http] body read error:", e)
                return None, False, "readinto"
            if not n:
                break
            pos += n
        if pos < clen:
            return bytes(pcm[:pos]), True, "readinto"
        return bytes(pcm), True, "readinto"

    pcm = bytearray()
    while True:
        if time.ticks_diff(deadline_ms, time.ticks_ms()) <= 0:
            print("[http] body deadline at {}B (no clen)".format(len(pcm)))
            return None, False, "extend"
        try:
            chunk = raw.read(CHUNK)
        except Exception as e:
            print("[http] body read error:", e)
            return None, False, "extend"
        if not chunk:
            break
        pcm.extend(chunk)
    return bytes(pcm), True, "extend"


def _try_set_socket_timeout(raw, timeout_s):
    """Best-effort socket timeout. requests2 builds differ in how (or
    whether) they expose the socket; silently skip if none of the common
    attribute names works."""
    for attr in ("sock", "s", "_sock"):
        sock = getattr(raw, attr, None)
        if sock is None:
            continue
        try:
            sock.settimeout(timeout_s)
            return True
        except Exception:
            continue
    try:
        raw.settimeout(timeout_s)
        return True
    except Exception:
        return False


def _get_header(headers, name):
    """Read an HTTP header robustly across requests2 versions.

    .headers can be: a dict with original-case keys, a dict with lowercased
    keys, a list of (key, value) tuples, or not present. Try each shape
    until one matches case-insensitively."""
    if not headers:
        return ""
    target = name.lower()
    if hasattr(headers, "get"):
        for k in (name, target, name.upper()):
            try:
                v = headers.get(k)
                if v:
                    return v
            except Exception:
                pass
    try:
        for k, v in headers.items():
            if str(k).lower() == target:
                return v
    except Exception:
        pass
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
    """Build the 44-byte canonical RIFF/WAVE PCM header in memory."""
    byte_rate = sample_rate * num_channels * (bits_per_sample // 8)
    block_align = num_channels * (bits_per_sample // 8)
    h = bytearray(44)
    h[0:4]   = b'RIFF'
    h[4:8]   = (36 + data_size).to_bytes(4, 'little')
    h[8:12]  = b'WAVE'
    h[12:16] = b'fmt '
    h[16:20] = (16).to_bytes(4, 'little')
    h[20:22] = (1).to_bytes(2, 'little')
    h[22:24] = (num_channels).to_bytes(2, 'little')
    h[24:28] = (sample_rate).to_bytes(4, 'little')
    h[28:32] = (byte_rate).to_bytes(4, 'little')
    h[32:34] = (block_align).to_bytes(2, 'little')
    h[34:36] = (bits_per_sample).to_bytes(2, 'little')
    h[36:40] = b'data'
    h[40:44] = (data_size).to_bytes(4, 'little')
    return h


# ===== Speaker =====

def _play_pcm(pcm):
    """Play raw 16 kHz / 16-bit / mono PCM through the speaker, capped by
    MAX_PLAYBACK_S so a stuck Speaker.isPlaying() flag can't trap the worker.
    Mic and Speaker share I2S, so we own Speaker.begin/end here."""
    Speaker.begin()
    Speaker.setVolumePercentage(100)
    Speaker.playRaw(memoryview(pcm), SAMPLE_RATE)
    deadline = time.ticks_add(time.ticks_ms(), MAX_PLAYBACK_S * 1000)
    while Speaker.isPlaying():
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            print("[voice] playback cap hit, forcing stop")
            break
        time.sleep_ms(20)
    Speaker.end()

# Standalone voice-assistant test app for M5Stack Core S3.
#
# Hold REC, ask a question, release. The WAV is uploaded to the Flask backend
# at /voice-assistant; the backend transcribes (Google STT), routes the intent
# (Gemini Flash), looks up data in BigQuery / OpenWeather, and replies with
# raw 16-bit/16kHz/mono PCM. We display the reply text and play the audio
# directly via Speaker.playRaw.
#
# Prereq: WiFi already connected (run wifi_manager.py or boot the main app
# once to provision credentials in NVS).

import _thread
import time

import M5
from M5 import Mic, Speaker, Widgets
import lvgl as lv
import m5ui
import requests2

# --- Audio config ---
SAMPLE_RATE = 16000
MAX_RECORD_TIME_SEC = 5
BYTES_PER_SAMPLE = 2

# --- Backend config ---
VOICE_URL = 'https://flask-app-868833155300.europe-west6.run.app/voice-assistant'
#VOICE_URL = 'http://0.0.0.0:8080/voice-assistant/'
SHARED_SECRET = '03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4'
HTTP_TIMEOUT_S = 30  # STT + LLM + BQ + TTS round-trip

# --- UI globals ---
page0 = None
label_status = None
label_reply = None
btn_rec = None

# --- Recording state ---
rec_data = None
recorded_bytes = 0
start_time = 0
busy = False  # guards against double-tap while a request is in flight


def save_wav(filename, pcm_data, sample_rate, num_channels=1, bits_per_sample=16):
    """Wraps raw PCM data with a WAV header and saves it to storage."""
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


def set_status(text):
    try:
        label_status.setText("Status: " + text)
    except Exception:
        pass


def set_reply(text):
    try:
        # crude line wrap: split every ~30 chars
        chunks = [text[i:i + 30] for i in range(0, len(text), 30)] or [""]
        label_reply.setText("\n".join(chunks[:5]))
    except Exception:
        pass


def ask_backend_thread(_arg):
    """Runs in a worker thread: upload WAV, play PCM reply."""
    global busy
    try:
        set_status("Uploading...")
        with open("/flash/recording.wav", "rb") as f:
            wav = f.read()

        try:
            resp = requests2.post(
                VOICE_URL,
                data=wav,
                headers={
                    "Content-Type": "audio/wav",
                    "X-Shared-Secret": SHARED_SECRET,
                },
                timeout=HTTP_TIMEOUT_S,
            )
        except Exception as e:
            print("[voice] network error:", e)
            set_status("Network error")
            set_reply(str(e))
            return

        if resp.status_code != 200:
            print("[voice] HTTP", resp.status_code)
            set_status("Error " + str(resp.status_code))
            try:
                set_reply(resp.text[:200])
            except Exception:
                set_reply("")
            resp.close()
            return

        transcript = resp.headers.get("X-Transcript", "") if hasattr(resp, "headers") else ""
        reply_text = resp.headers.get("X-Response-Text", "") if hasattr(resp, "headers") else ""
        pcm = resp.content
        resp.close()

        if transcript:
            print("[voice] heard:", transcript)
        if reply_text:
            print("[voice] reply:", reply_text)
        set_reply(reply_text or "(no text)")
        set_status("Playing...")

        # Speaker output: playRaw expects raw signed-16 PCM at the given rate.
        Speaker.begin()
        Speaker.setVolumePercentage(100)
        Speaker.playRaw(memoryview(pcm), SAMPLE_RATE)
        while Speaker.isPlaying():
            time.sleep_ms(20)
        Speaker.end()

        set_status("Ready")
    except Exception as e:
        print("[voice] error:", e)
        set_status("Error")
        set_reply(str(e))
    finally:
        busy = False


def rec_event_handler(event_struct):
    global rec_data, recorded_bytes, start_time, busy
    event = event_struct.code

    if event == lv.EVENT.PRESSED:
        if busy:
            return
        set_status("Recording...")
        btn_rec.set_bg_color(0x990000, 255, 0)
        Speaker.end()  # mic and speaker share the I2S bus on Core S3

        Mic.begin()
        start_time = time.ticks_ms()
        Mic.record(rec_data, SAMPLE_RATE, False)

    elif event == lv.EVENT.RELEASED:
        if busy:
            return
        Mic.end()
        btn_rec.set_bg_color(0xFF0000, 255, 0)

        elapsed_ms = time.ticks_diff(time.ticks_ms(), start_time)
        recorded_bytes = int((elapsed_ms / 1000) * SAMPLE_RATE * BYTES_PER_SAMPLE)
        if recorded_bytes > len(rec_data):
            recorded_bytes = len(rec_data)
        if recorded_bytes < SAMPLE_RATE * BYTES_PER_SAMPLE // 4:  # <0.25s
            set_status("Too short, try again")
            return

        set_status("Saving WAV...")
        valid_audio = memoryview(rec_data)[:recorded_bytes]
        save_wav("/flash/recording.wav", valid_audio, SAMPLE_RATE)

        busy = True
        _thread.start_new_thread(ask_backend_thread, (None,))


def setup():
    global page0, label_status, label_reply, btn_rec, rec_data

    M5.begin()
    m5ui.init()

    rec_data = bytearray(SAMPLE_RATE * BYTES_PER_SAMPLE * MAX_RECORD_TIME_SEC)

    page0 = m5ui.M5Page(bg_c=0x222222)

    btn_rec = m5ui.M5Button(
        text="HOLD TO ASK",
        x=60, y=160,
        bg_c=0xFF0000, text_c=0xFFFFFF,
        font=lv.font_montserrat_14,
        parent=page0,
    )
    btn_rec.set_size(200, 70)
    btn_rec.add_event_cb(rec_event_handler, lv.EVENT.ALL, None)

    page0.screen_load()

    label_status = Widgets.Label("Status: Ready", 10, 10, 1.0, 0xFFFFFF, 0x222222, Widgets.FONTS.DejaVu18)
    label_reply = Widgets.Label("", 10, 50, 1.0, 0x80FF80, 0x222222, Widgets.FONTS.DejaVu18)

    Speaker.begin()
    Speaker.setVolumePercentage(100)
    Speaker.end()


def loop():
    M5.update()


if __name__ == "__main__":
    try:
        setup()
        while True:
            loop()
    except (Exception, KeyboardInterrupt) as e:
        try:
            m5ui.deinit()
            from utility import print_error_msg
            print_error_msg(e)
        except ImportError:
            print("Please update to the latest firmware")

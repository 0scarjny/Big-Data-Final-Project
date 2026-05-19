# RGB LED status indicator for the M5 Stack.
#
# Three patterns, all triggered by cloud.send_data:
#   flash_sending() — 5 rapid green blinks while the upload is in flight
#   flash_success() — 1-second green hold on a 200 response
#   flash_error()   — 2-second red hold on a 4xx/5xx/network failure
#
# Coroutines: pauses yield to the asyncio loop so other tasks keep running
# during the LED animation. If init() fails (no RGB attached, wrong firmware),
# every flash function becomes a no-op.

import asyncio

try:
    from hardware import RGB
    _RGB_AVAILABLE = True
except ImportError:
    _RGB_AVAILABLE = False

# Defaults match the demo snippet from the user (10-LED SK6812 strip on GPIO 25).
DEFAULT_PIN = 25
DEFAULT_COUNT = 10
DEFAULT_BRIGHTNESS = 60

GREEN = 0x00FF00
RED = 0xFF0000
OFF = 0x000000

_rgb = None
_enabled = True


def set_enabled(flag):
    global _enabled
    _enabled = bool(flag)


def init(pin=DEFAULT_PIN, n=DEFAULT_COUNT, brightness=DEFAULT_BRIGHTNESS):
    """Initialise the LED strip. Safe to call once at boot. If the firmware
    doesn't expose hardware.RGB or the strip isn't wired up, the module
    falls back to no-op so the app keeps running."""
    global _rgb
    if not _RGB_AVAILABLE:
        print("[led] hardware.RGB not available — patterns disabled")
        return False
    if _rgb is not None:
        return True
    try:
        _rgb = RGB(io=pin, n=n, type="SK6812")
        _rgb.set_brightness(brightness)
        _rgb.fill_color(OFF)
        print("[led] init ok pin={} n={}".format(pin, n))
        return True
    except Exception as e:
        print("[led] init failed:", e)
        _rgb = None
        return False


def _set(color):
    if _rgb is None:
        return
    try:
        _rgb.fill_color(color)
    except Exception as e:
        print("[led] set error:", e)


async def flash_sending():
    """5 rapid green blinks (~1 s total) — 'uploading to BigQuery'."""
    if _rgb is None or not _enabled:
        return
    for _ in range(5):
        _set(GREEN)
        await asyncio.sleep_ms(100)
        _set(OFF)
        await asyncio.sleep_ms(100)


async def flash_success():
    """1-second green hold — 200 response from the backend."""
    if _rgb is None or not _enabled:
        return
    _set(GREEN)
    await asyncio.sleep(1)
    _set(OFF)


async def flash_error():
    """2-second red hold — 4xx/5xx response or network failure."""
    if _rgb is None or not _enabled:
        return
    _set(RED)
    await asyncio.sleep(2)
    _set(OFF)

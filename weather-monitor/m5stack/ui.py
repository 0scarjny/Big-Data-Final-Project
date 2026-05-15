import time
import _thread
import asyncio
import lvgl as lv
import m5ui

import voice_client
import forecast as forecast_mod

DEBUG_UI = True


_wlan_sta = None

# Pages (0-indexed):
#   0: dashboard
#   1: forecast (today / 5-day toggle)
#   2: voice assistant
#   3: configuration (QR + AP credentials)
page0 = None
page1 = None
page2 = None
page3 = None
current_page = 0

# Page-change hook (registered by main.py). Fired on every navigation with
# (prev_page, current_page) so main.py can lazy-start AP + config server only
# while page 3 is on screen.
_page_change_hook = None


def set_page_change_hook(cb):
    global _page_change_hook
    _page_change_hook = cb

# Page 0 (dashboard) widgets
temp_val_label = None
hum_val_label  = None   # small "45%" label above the comfort arrow
co2_label      = None
comfort_arrow  = None
ampm_label     = None
time_hm_label  = None
seconds_label  = None
date_label     = None
wifi_ind       = None
location_label = None

# Page 1 (forecast) widgets — built once, populated each refresh
forecast_title = None
forecast_subtitle = None
forecast_toggle_btn = None
forecast_toggle_label = None
forecast_status = None
# Per-slot widgets: top label (hour/day), icon, temp label
forecast_slots = []   # list of dicts: {"top", "icon", "temp"}
FORECAST_SLOTS = 5    # used as the wider container; today view fills 5 of 6
_forecast_view = "today"   # "today" or "week"
_forecast_data = None      # last successful raw forecast dict
_forecast_fetching = False

# Page 2 (voice) widgets
voice_btn_rec      = None   # face circle (lv.obj) — hold to record
voice_label_status = None   # status label below face
voice_label_reply  = None   # reply label inside bubble (m5ui.M5Label)
voice_spinner      = None   # kept None; spinner cb drives face colour

# Page 3 (Wi-Fi configuration) widgets
cfg_status_label = None
cfg_qr = None
cfg_qr_fallback_label = None
cfg_ssid_label = None
cfg_url_label = None


def _ui_log(*args):
    if DEBUG_UI:
        print("[ui]", *args)


# ---------------------------------------------------------------------------
# Thread-safety harness
#
# LVGL is touched only from the asyncio "main" thread. Worker threads call the
# public set_*/update_* wrappers, which write into _pending under
# _pending_lock and signal _dirty_flag. The ui_render_task coroutine in
# main.py drains _pending on its next tick via flush_pending(), which calls
# the private _apply_* helpers — those perform the actual LVGL writes and are
# guarded by _assert_main().
# ---------------------------------------------------------------------------

_main_thread_id = None
_pending = {}
_pending_lock = _thread.allocate_lock()
# _signal_pending is True from the moment a worker calls _dirty_flag.set() up
# until flush_pending() runs. While it's True, additional _queue() calls just
# write into _pending and skip the .set() — this coalescing is essential:
# ThreadSafeFlag.set() uses micropython.schedule() internally, whose queue is
# only 8 slots. Without coalescing, a busy worker can flood it and m5ui's
# LVGL tick callback dies with `RuntimeError: schedule queue full`.
_signal_pending = False
try:
    _dirty_flag = asyncio.ThreadSafeFlag()
    _has_threadsafe_flag = True
except Exception:
    _dirty_flag = None
    _has_threadsafe_flag = False


def _assert_main():
    """Diagnostic guard: prints a loud warning if an LVGL write happens off
    the main thread. After step 2 of the snappy-lantern plan this should
    never fire — until then it's a regression beacon."""
    if _main_thread_id is None:
        return
    tid = _thread.get_ident()
    if tid != _main_thread_id:
        print("[ui] WRONG THREAD: tid={} expected={}".format(tid, _main_thread_id))


def _queue(key, value=True):
    """Thread-safe enqueue. The check-and-flip of _signal_pending lives inside
    the lock so two workers can never both call _dirty_flag.set(). At most one
    micropython.schedule() slot is consumed between flushes, regardless of
    update rate."""
    global _signal_pending
    need_signal = False
    _pending_lock.acquire()
    try:
        _pending[key] = value
        if not _signal_pending:
            _signal_pending = True
            need_signal = True
    finally:
        _pending_lock.release()
    if need_signal and _has_threadsafe_flag:
        try:
            _dirty_flag.set()
        except Exception:
            pass


async def wait_dirty():
    """Awaited by main.ui_render_task. Parks on the ThreadSafeFlag when
    available; falls back to a 50 ms poll on firmwares that don't expose
    it."""
    if _has_threadsafe_flag:
        await _dirty_flag.wait()
    else:
        await asyncio.sleep_ms(50)


def flush_pending():
    """Drain queued state into LVGL. Main-thread only."""
    _assert_main()
    global _pending, _signal_pending
    _pending_lock.acquire()
    try:
        snapshot = _pending
        _pending = {}
        # Reset under the lock so any worker that observed _signal_pending=True
        # mid-flush is guaranteed to have its write captured in `snapshot`; any
        # worker arriving after this re-arms a fresh schedule slot.
        _signal_pending = False
    finally:
        _pending_lock.release()
    # MicroPython dicts preserve insertion order; the most recent enqueue per
    # key wins because _queue overwrites by key.
    for key, val in snapshot.items():
        try:
            if key == 'temperature':         _apply_temperature(val)
            elif key == 'humidity':          _apply_humidity(val)
            elif key == 'co2':               _apply_co2(val)
            elif key == 'location':          _apply_location(val)
            elif key == 'clock':             _apply_clock()
            elif key == 'wifi_indicator':    _apply_wifi_indicator()
            elif key == 'forecast_data':     _apply_forecast_data(val)
            elif key == 'forecast_loading':  _apply_forecast_loading()
            elif key == 'cfg_ap':            _apply_cfg_ap(val)
            elif key == 'cfg_connected':     _apply_cfg_connected(*val)
            elif key == 'cfg_disconnected':  _apply_cfg_disconnected()
            elif key == 'voice_status':      _apply_voice_status(val)
            elif key == 'voice_reply':       _apply_voice_reply(val)
            elif key == 'voice_spinner':     _apply_voice_spinner(val)
        except Exception as e:
            _ui_log("apply error:", key, e)


def safe_font(size):
    name = "font_montserrat_{}".format(size)
    return getattr(lv, name, lv.font_montserrat_14)


# Date/time formatting lookup tables
_DAYS   = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# ----------------------------------------------------------------------------
# Page builders
# ----------------------------------------------------------------------------

def _build_dashboard_page(font_big, font_small, font_tiny, font_medium, font_large, text_color, bg_color):
    global page0
    global wifi_ind, date_label
    global location_label, time_hm_label, seconds_label, ampm_label
    global temp_val_label, hum_val_label, co2_label, comfort_arrow

    page0 = m5ui.M5Page(bg_c=bg_color)

    # --- Header (WiFi left, Date center, City right) ---
    wifi_ind = m5ui.M5Label(lv.SYMBOL.WIFI, x=5, y=5,
                             text_c=0xE74C3C, bg_opa=0, font=safe_font(14), parent=page0)

    date_label = m5ui.M5Label("--- --", x=80, y=5,
                               text_c=text_color, bg_opa=0, font=font_small, parent=page0)
    date_label.set_size(160, 18)
    try:
        date_label.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
    except Exception:
        pass

    location_label = m5ui.M5Label("", x=240, y=5,
                                   text_c=text_color, bg_opa=0, font=font_small, parent=page0)
    location_label.set_size(75, 18)
    try:
        location_label.set_style_text_align(lv.TEXT_ALIGN.RIGHT, 0)
    except Exception:
        pass

    # --- Main Time Card (h=101, 10% reduction from 112; 2px black border) ---
    card_main = lv.obj(page0)
    card_main.set_pos(5, 27)
    card_main.set_size(310, 101)
    card_main.set_style_bg_color(lv.color_hex(0xFFFFFF), 0)
    card_main.set_style_bg_opa(255, 0)
    card_main.set_style_radius(12, 0)
    card_main.set_style_border_width(2, 0)
    card_main.set_style_border_color(lv.color_hex(0x000000), 0)
    card_main.set_style_pad_all(0, 0)

    # Time vertically centered in card: (101-64)/2 = 18 → y = 27+18 = 45
    # HH:MM in roboto_64 left of stack; AM/PM (top) + seconds (bottom) right.
    time_hm_label = m5ui.M5Label("--:--", x=25, y=45,
                                  text_c=text_color, bg_opa=0, font=font_big, parent=page0)
    ampm_label = m5ui.M5Label("AM", x=245, y=55,
                               text_c=text_color, bg_opa=0, font=font_large, parent=page0)
    seconds_label = m5ui.M5Label("00", x=245, y=90,
                                  text_c=text_color, bg_opa=0, font=font_large, parent=page0)

    # --- Sensor Cards: temp (left) + CO2 (right). h=56 absorbs the 11px ---
    # freed by the main-card shrink. 2px black border to match.
    CARD_Y = 132
    CARD_W, CARD_H = 152, 56
    SENSOR_CX = [5, 163]   # two cards; 6px gap, 5px right margin

    for sx in SENSOR_CX:
        c = lv.obj(page0)
        c.set_pos(sx, CARD_Y)
        c.set_size(CARD_W, CARD_H)
        c.set_style_bg_color(lv.color_hex(0xFFFFFF), 0)
        c.set_style_bg_opa(255, 0)
        c.set_style_radius(10, 0)
        c.set_style_border_width(2, 0)
        c.set_style_border_color(lv.color_hex(0x000000), 0)
        c.set_style_pad_all(0, 0)

    LABEL_H = 28
    LABEL_Y = CARD_Y + (CARD_H - LABEL_H) // 2

    def _sensor_label(text, cx):
        lbl = m5ui.M5Label(text, x=cx + 2, y=LABEL_Y,
                           text_c=text_color, bg_opa=0, font=font_medium, parent=page0)
        lbl.set_size(CARD_W - 4, LABEL_H)
        try:
            lbl.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
        except Exception:
            pass
        return lbl

    temp_val_label = _sensor_label("--°C",   SENSOR_CX[0])
    co2_label      = _sensor_label("-- ppm", SENSOR_CX[1])

    # --- Comfort bar section (y=193-240) ---
    BAR_Y  = 214
    LABL_Y = 220

    # Bars span the full width of the main bubble (x=5..x=315 = 310 px),
    # three 100px bars with 5px gaps: 100 + 5 + 100 + 5 + 100 = 310.
    bar_dry     = m5ui.M5Label("", x=5,   y=BAR_Y, bg_c=0xF4A42D, bg_opa=255, parent=page0)
    bar_dry.set_size(103, 24)
    bar_comfort = m5ui.M5Label("", x=108, y=BAR_Y, bg_c=0x27AE60, bg_opa=255, parent=page0)
    bar_comfort.set_size(103, 24)
    bar_wet     = m5ui.M5Label("", x=211, y=BAR_Y, bg_c=0x2980B9, bg_opa=255, parent=page0)
    bar_wet.set_size(103, 24)

    m5ui.M5Label("DRY",     x=43,  y=LABL_Y, text_c=0xFFFFFF, bg_opa=0, font=font_tiny, parent=page0)
    m5ui.M5Label("COMFORT", x=130, y=LABL_Y, text_c=0xFFFFFF, bg_opa=0, font=font_tiny, parent=page0)
    m5ui.M5Label("WET",     x=248, y=LABL_Y, text_c=0xFFFFFF, bg_opa=0, font=font_tiny, parent=page0)

    # humidity % label and arrow — x repositioned dynamically by _apply_humidity
    hum_val_label = m5ui.M5Label("--", x=150, y=193,
                                  text_c=text_color, bg_opa=0, font=font_tiny, parent=page0)
    comfort_arrow = m5ui.M5Label(lv.SYMBOL.DOWN, x=155, y=205,
                                  text_c=text_color, bg_opa=0, font=safe_font(14), parent=page0)


def _build_forecast_page(font_title, font_tiny, font_weather, text_color, bg_color):
    """Forecast page: title at top, 5 slots in the middle (icon + label +
    temp), and a centered toggle button at the bottom that flips between
    'Today' (next 5 three-hour buckets) and 'Week' (5-day summary).

    Icons render as glyphs from the Erik Flowers Weather Icons font (loaded
    in init() as font_weather). When the font fails to load, the slot's
    icon is None and the icon row stays blank — labels still render."""
    global page1, forecast_title, forecast_subtitle
    global forecast_toggle_btn, forecast_toggle_label, forecast_status, forecast_slots

    page1 = m5ui.M5Page(bg_c=bg_color)
    page1.set_flag(lv.obj.FLAG.SCROLLABLE, False)

    forecast_title = m5ui.M5Label("Forecast — Today", x=12, y=8,
                                   text_c=text_color, bg_opa=0, font=font_title, parent=page1)
    forecast_subtitle = m5ui.M5Label("--", x=12, y=34,
                                      text_c=0x555555, bg_opa=0, font=font_tiny, parent=page1)

    # roboto_18 is used for the week-view min/max temps (blue/red) — a touch
    # smaller than the page's font_tiny so two values fit side-by-side.
    try:
        font_week_temp = lv.binfont_create("S:/flash/res/font/roboto_18.bin")
        if font_week_temp is None:
            raise OSError("returned None")
    except Exception as e:
        _ui_log("roboto_18 load failed:", e)
        font_week_temp = font_tiny
    globals()["_font_week_temp_ref"] = font_week_temp

    # 5 evenly spaced columns across the 320 px width.
    # Every label below is sized to slot_w with CENTER text alignment so the
    # hour/day, icon glyph and temperature share the same horizontal anchor.
    forecast_slots = []
    slot_w = 64
    margin = (320 - slot_w * FORECAST_SLOTS) // 2 # ~0
    for i in range(FORECAST_SLOTS):
        x0 = margin + i * slot_w

        top = m5ui.M5Label("", x=x0, y=58,
                            text_c=text_color, bg_opa=0, font=font_tiny, parent=page1)
        top.set_size(slot_w, 18)
        try:
            top.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
        except Exception:
            pass

        if font_weather is not None:
            icon = m5ui.M5Label("",
                                 x=x0, y=80,
                                 text_c=text_color, bg_opa=0,
                                 font=font_weather, parent=page1)
            icon.set_size(slot_w, 56)
            try:
                icon.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
            except Exception:
                pass
        else:
            icon = None

        # Today-view single temperature (centered, default colour).
        temp = m5ui.M5Label("--", x=x0, y=152,
                             text_c=text_color, bg_opa=0, font=font_tiny, parent=page1)
        temp.set_size(slot_w, 18)
        try:
            temp.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
        except Exception:
            pass

        # Week-view min (blue) and max (red), side-by-side. Hidden until
        # _render_week() shows them; _render_today() hides them again.
        temp_min = m5ui.M5Label("--", x=x0 + 2, y=152,
                                 text_c=0x2980B9, bg_opa=0,
                                 font=font_week_temp, parent=page1)
        temp_min.set_size(28, 20)
        try:
            temp_min.set_style_text_align(lv.TEXT_ALIGN.RIGHT, 0)
        except Exception:
            pass
        try:
            temp_min.set_flag(lv.obj.FLAG.HIDDEN, True)
        except Exception:
            pass

        temp_max = m5ui.M5Label("--", x=x0 + 34, y=152,
                                 text_c=0xE74C3C, bg_opa=0,
                                 font=font_week_temp, parent=page1)
        temp_max.set_size(28, 20)
        try:
            temp_max.set_style_text_align(lv.TEXT_ALIGN.LEFT, 0)
        except Exception:
            pass
        try:
            temp_max.set_flag(lv.obj.FLAG.HIDDEN, True)
        except Exception:
            pass

        forecast_slots.append({
            "top": top, "icon": icon, "temp": temp,
            "temp_min": temp_min, "temp_max": temp_max,
            "x0": x0,
        })

    forecast_status = m5ui.M5Label("", x=12, y=174,
                                    text_c=0x555555, bg_opa=0, font=font_tiny, parent=page1)

    forecast_toggle_btn = m5ui.M5Button(text="Change view", x=90, y=195, w=160, h=34,
                                         bg_c=0x2980B9, text_c=0xFFFFFF,
                                         font=font_tiny, parent=page1)
    forecast_toggle_btn.add_event_cb(_on_forecast_toggle, lv.EVENT.CLICKED, None)


def _build_voice_page(font_small, font_tiny, text_color, bg_color):
    global page2, voice_btn_rec, voice_label_status, voice_label_reply, voice_spinner

    # ── Page ──────────────────────────────────────────────────────────────
    page2 = m5ui.M5Page(bg_c=bg_color)
    page2.set_flag(lv.obj.FLAG.SCROLLABLE, False)  # M5Page wrapper — set_flag OK

    # ── Title ─────────────────────────────────────────────────────────────
    # Use M5Label so the m5ui set_flag wrapper is available if needed.
    m5ui.M5Label(lv.SYMBOL.AUDIO + " Ask Assistant",
                 x=0, y=2, text_c=text_color, bg_opa=0, font=safe_font(14), parent=page2)

    # ── Speech bubble background (M5Label = styled white rounded rect) ────
    bubble_bg = m5ui.M5Label("", x=8, y=18, bg_c=0xFFFFFF, bg_opa=255, parent=page2)
    bubble_bg.set_size(304, 118)
    try:
        bubble_bg.set_style_radius(12, 0)
        bubble_bg.set_style_border_width(1, 0)
        bubble_bg.set_style_border_color(lv.color_hex(0xBBBBBB), 0)
    except Exception:
        pass  # styling is cosmetic — don't crash if API varies

    # ── Reply text (absolute on page, visually inside bubble) ─────────────
    # Positioned at (18, 26) = bubble (8,18) + inner padding (10, 8).
    # Use M5Label so set_long_mode delegates to the underlying lv.label.
    voice_label_reply = m5ui.M5Label(
        "Hold the mic to ask me anything...",
        x=18, y=26, text_c=0x444444, bg_opa=0, font=font_tiny, parent=page2,
    )
    voice_label_reply.set_size(284, 102)
    try:
        voice_label_reply.set_long_mode(lv.label.LONG.WRAP)
        voice_label_reply.set_style_text_align(lv.TEXT_ALIGN.LEFT, 0)
    except Exception:
        pass  # fall back gracefully if long_mode API differs


    # ── Avatar face circle (raw lv.obj — hold to record) ─────────────────
    # Intentionally no flag calls: add_flag / clear_flag are not exposed on
    # raw lv.obj in this UIFlow build (only on m5ui wrappers). Scrollbars
    # won't appear because the circle has no overflow children.
    FACE_SIZE = 96
    face_x = (320 - FACE_SIZE) // 2  # 128 — centred
    voice_btn_rec = lv.obj(page2)
    voice_btn_rec.set_pos(face_x, 140)
    voice_btn_rec.set_size(FACE_SIZE, FACE_SIZE)
    voice_btn_rec.set_style_radius(FACE_SIZE // 2, 0)
    voice_btn_rec.set_style_bg_color(lv.color_hex(0xF9D87A), 0)
    voice_btn_rec.set_style_bg_opa(255, 0)
    voice_btn_rec.set_style_border_width(2, 0)
    voice_btn_rec.set_style_border_color(lv.color_hex(0x555555), 0)
    voice_btn_rec.set_style_pad_all(0, 0)
    voice_btn_rec.add_event_cb(_on_voice_button_event, lv.EVENT.ALL, None)

    # ── Status label (below face) ─────────────────────────────────────────
    voice_label_status = m5ui.M5Label(
        "Ready", x=0, y=180, text_c=text_color, bg_opa=0, font=font_tiny, parent=page2,
    )
    voice_label_status.set_size(320, 14)
    try:
        voice_label_status.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
    except Exception:
        pass

    voice_spinner = None  # removed; spinner callback now drives face colour

    voice_client.prepare()
    voice_client.register_callbacks(_set_voice_status, _set_voice_reply, _show_voice_spinner)


def _make_qrcode(parent, size, x, y):
    """Create an LVGL QR code widget. Wraps API differences across LVGL 8/9
    builds; returns (widget, fallback_label). Exactly one of the two will be
    non-None — the other is None. The fallback label is shown when no QR
    constructor is available so the URL still reaches the user."""
    # LVGL 9 style: lv.qrcode(parent), then size + colors via setters.
    try:
        QrCls = getattr(lv, "qrcode", None)
        if QrCls is not None:
            try:
                qr = QrCls(parent)
                try: qr.set_size(size)
                except AttributeError: pass
                try:
                    qr.set_dark_color(lv.color_hex(0x000000))
                    qr.set_light_color(lv.color_hex(0xFFFFFF))
                except AttributeError:
                    pass
                qr.set_pos(x, y)
                return qr, None
            except TypeError:
                # LVGL 8 style: lv.qrcode(parent, size, dark, light)
                try:
                    qr = QrCls(parent, size, lv.color_hex(0x000000), lv.color_hex(0xFFFFFF))
                    qr.set_pos(x, y)
                    return qr, None
                except Exception as e:
                    _ui_log("LVGL 8 qrcode ctor failed:", e)
            except Exception as e:
                _ui_log("LVGL 9 qrcode ctor failed:", e)
    except Exception as e:
        _ui_log("qrcode lookup raised:", e)

    # Fallback: large URL label centred where the QR would have been.
    label = m5ui.M5Label("(QR unavailable)", x=x, y=y + size // 2 - 8,
                          text_c=0x000000, bg_opa=0, parent=parent)
    return None, label


def _build_config_page(font_small, font_tiny, text_color, bg_color):
    """Configuration page: QR code linking to the WifiManager2 HTTP form,
    plus the AP SSID/password and URL in plain text. Replaces the old scan
    list + on-screen keyboard."""
    global page3, cfg_status_label, cfg_qr, cfg_qr_fallback_label
    global cfg_ssid_label, cfg_url_label

    page3 = m5ui.M5Page(bg_c=bg_color)

    m5ui.M5Label("Configuration", x=12, y=8, text_c=text_color, bg_opa=0,
                 font=font_small, parent=page3)
    cfg_status_label = m5ui.M5Label("Starting...", x=12, y=28,
                                     text_c=0x555555, bg_opa=0, font=font_tiny, parent=page3)

    qr_size = 130
    qr_x = (320 - qr_size) // 2
    cfg_qr, cfg_qr_fallback_label = _make_qrcode(page3, size=qr_size, x=qr_x, y=50)

    cfg_ssid_label = m5ui.M5Label("", x=12, y=188, text_c=text_color, bg_opa=0, font=font_tiny, parent=page3)
    cfg_url_label  = m5ui.M5Label("", x=12, y=204, text_c=text_color, bg_opa=0, font=font_tiny, parent=page3)


def _set_qr(text):
    """Update the QR widget (or the text fallback) with a new URL."""
    _assert_main()
    if cfg_qr is not None:
        try:
            try:
                cfg_qr.update(text, len(text))
            except TypeError:
                # Some bindings expect bytes
                buf = text.encode("utf-8")
                cfg_qr.update(buf, len(buf))
            return
        except Exception as e:
            _ui_log("qr update failed:", e)
    if cfg_qr_fallback_label is not None:
        try:
            cfg_qr_fallback_label.set_text(text)
        except Exception as e:
            _ui_log("qr fallback set_text failed:", e)


def init(wlan_sta):
    global _wlan_sta, _main_thread_id
    _wlan_sta = wlan_sta

    def _load_roboto(size):
        try:
            f = lv.binfont_create("S:/flash/res/font/roboto_{}.bin".format(size))
            if f is None:
                raise OSError("returned None")
            return f
        except Exception as e:
            print("roboto_{} load failed, falling back:".format(size), e)
            return safe_font(size)

    font_small  = _load_roboto(20)
    font_medium = _load_roboto(30)
    font_tiny   = _load_roboto(18)
    font_large  = _load_roboto(30)
    font_big    = _load_roboto(80)
       
    globals()["_font_large_ref"]  = font_large
    globals()["_font_medium_ref"] = font_medium
    globals()["_font_small_ref"]  = font_small
    globals()["_font_tiny_ref"]   = font_tiny
    globals()["_font_big_ref"]    = font_big

    try:
        font_weather = lv.binfont_create("S:/flash/res/font/weather_icons_48.bin")
        if font_weather is None:
            raise OSError("binfont_create returned None")
    except Exception as e:
        print("Weather icon font load failed, falling back:", e)
        font_weather = None
    globals()["_font_weather_ref"] = font_weather

    text_color = 0x000000
    bg_color = 0xD1D1D1

    _build_dashboard_page(font_big, font_small, font_tiny, font_medium, font_large, text_color, bg_color)
    _build_forecast_page(font_medium, font_small, font_weather, text_color, bg_color)
    _build_voice_page(font_small, font_tiny, text_color, bg_color)
    _build_config_page(font_small, font_tiny, text_color, bg_color)

    page0.screen_load()

    # Capture the asyncio loop's thread id. init() is called from main() which
    # runs inside asyncio.run, so this IS the main thread we want every LVGL
    # write to come from.
    _main_thread_id = _thread.get_ident()


# ----------------------------------------------------------------------------
# Public, thread-safe setters (callable from any thread)
# ----------------------------------------------------------------------------

def set_temperature(v):        _queue('temperature', v)
def set_humidity(v):           _queue('humidity', v)
def set_co2(v):                _queue('co2', v)
def set_location(s):           _queue('location', s)
def update_clock():            _queue('clock')
def refresh_wifi_indicator():  _queue('wifi_indicator')
def update_forecast(data):     _queue('forecast_data', data)
def forecast_show_loading():   _queue('forecast_loading')


def set_config_status_ap(info):
    """Render the AP-mode UI: red status, QR + visible AP credentials."""
    _queue('cfg_ap', info)


def set_config_status_connected(ssid, ip, url):
    """Render the connected-mode UI: green status, QR points to the device IP
    so the same web form is reachable from a phone on the same LAN."""
    _queue('cfg_connected', (ssid, ip, url))


def set_config_status_disconnected():
    _queue('cfg_disconnected')


# Voice callbacks registered with voice_client.register_callbacks. The voice
# worker thread fires these; they queue and return immediately so no LVGL
# call ever happens off the main thread.
def _set_voice_status(text):
    _queue('voice_status', text)


def _set_voice_reply(text):
    _queue('voice_reply', text)


def _show_voice_spinner(visible):
    _queue('voice_spinner', bool(visible))


# ----------------------------------------------------------------------------
# Private appliers — main-thread only, perform the actual LVGL writes.
# ----------------------------------------------------------------------------

def _apply_temperature(temperature):
    _assert_main()
    temp_val_label.set_text("{:.1f}°C".format(temperature))


def _apply_humidity(humidity):
    _assert_main()
    clamped = max(0, min(100, humidity))
    arrow_x = 5 + int((clamped / 100.0) * 310)
    comfort_arrow.set_pos(arrow_x - 5, 205)
    hum_val_label.set_pos(arrow_x - 10, 193)
    hum_val_label.set_text("{:.0f}%".format(humidity))


def _apply_co2(co2):
    _assert_main()
    co2_label.set_text("{} ppm".format(co2))


def _apply_location(location_str):
    _assert_main()
    location_label.set_text(location_str.upper() if location_str else "")


def _apply_clock():
    _assert_main()
    t = time.localtime()
    hour, minute, second, month, day, dow = t[3], t[4], t[5], t[1], t[2], t[6]
    h12 = hour % 12 or 12
    ampm_label.set_text("AM" if hour < 12 else "PM")
    time_hm_label.set_text("{:02d}:{:02d}:".format(h12, minute))
    seconds_label.set_text("{:02d}".format(second))
    date_label.set_text("{}, {} {}".format(_DAYS[dow], _MONTHS[month - 1], day))


def _apply_wifi_indicator():
    """Recolour the dashboard Wi-Fi icon based on the STA association.
    Status text on the config page is driven by the cfg_* appliers — this
    function only owns the icon."""
    _assert_main()
    try:
        if _wlan_sta is not None and _wlan_sta.isconnected():
            wifi_ind.set_text_color(0x2ECC71, 255, lv.PART.MAIN)
        else:
            wifi_ind.set_text_color(0xE74C3C, 255, lv.PART.MAIN)
    except Exception as e:
        _ui_log("refresh_wifi_indicator error:", e)


def _apply_forecast_data(data):
    """Stores the raw data and re-renders if the user is currently looking at
    this page."""
    _assert_main()
    global _forecast_data
    _forecast_data = data
    if data is None:
        forecast_status.set_text("Forecast unavailable")
        return
    if is_forecast_active():
        _render_forecast()


def _apply_forecast_loading():
    _assert_main()
    forecast_status.set_text("Loading forecast...")


def _apply_cfg_ap(info):
    _assert_main()
    try:
        cfg_status_label.set_text("AP mode - connect to set up Wi-Fi")
        cfg_status_label.set_text_color(0xC0392B, 255, lv.PART.MAIN)
        _set_qr(info["url"])
        cfg_ssid_label.set_text("Wi-Fi: " + info.get("essid", ""))
        cfg_url_label.set_text(info.get("url", ""))
    except Exception as e:
        _ui_log("set_config_status_ap error:", e)


def _apply_cfg_connected(ssid, ip, url):
    _assert_main()
    try:
        cfg_status_label.set_text("Connected - " + ssid)
        cfg_status_label.set_text_color(0x27AE60, 255, lv.PART.MAIN)
        _set_qr(url)
        cfg_ssid_label.set_text("IP: " + ip)
        cfg_url_label.set_text(url)
    except Exception as e:
        _ui_log("set_config_status_connected error:", e)


def _apply_cfg_disconnected():
    _assert_main()
    try:
        cfg_status_label.set_text("Disconnected - searching...")
        cfg_status_label.set_text_color(0xE67E22, 255, lv.PART.MAIN)
    except Exception as e:
        _ui_log("set_config_status_disconnected error:", e)


def _apply_voice_status(text):
    """Update the status label and the face colour. Three stages only:
        Listening -> Asking -> Speaking -> Ready
    Plus the obvious error / timeout states."""
    _assert_main()
    try:
        voice_label_status.set_text(text)
    except Exception as e:
        _ui_log("voice status err:", e)
    if text in ("Ready", "Timed out", "Too short, try again"):
        _set_face_state("idle")
    elif text == "Recording...":
        _set_face_state("recording")
    elif text == "Asking...":
        _set_face_state("thinking")
    elif text == "Speaking...":
        _set_face_state("speaking")
    elif text.startswith("Error") or text == "Network error":
        _set_face_state("error")


def _apply_voice_reply(text):
    _assert_main()
    try:
        # LVGL LONG.WRAP on the label handles word-wrap; no manual chunking needed.
        voice_label_reply.set_text(
            text if text else "Hold the mic to ask me anything\xe2\x80\xa6"
        )
    except Exception as e:
        _ui_log("voice reply err:", e)


def _apply_voice_spinner(visible):
    # Spinner widget removed (was laggy). Drive face expression instead.
    _assert_main()
    _set_face_state("thinking" if visible else "idle")


# ----------------------------------------------------------------------------
# Forecast page setters / event handlers
# ----------------------------------------------------------------------------

def is_forecast_active():
    return current_page == 1


def _format_temp(value):
    if value is None:
        return "--"
    return "{}°".format(int(round(value)))


def _set_slot_icon(slot, code):
    """Replace the slot's icon glyph. The label is the same instance so
    layout stays stable; only the displayed character changes."""
    _assert_main()
    icon = slot.get("icon")
    if icon is None:
        return
    try:
        icon.set_text(forecast_mod.icon_glyph(code))
    except Exception as e:
        _ui_log("set_slot_icon err:", e)


def _set_slot_hidden(slot, hidden, week_view=False):
    """Hide/show a slot's widgets. In week view the single `temp` is hidden
    and the min/max pair is shown; in today view it's the opposite."""
    try:
        slot["top"].set_flag(lv.obj.FLAG.HIDDEN, hidden)
        if slot["icon"] is not None:
            slot["icon"].set_flag(lv.obj.FLAG.HIDDEN, hidden)
        slot["temp"].set_flag(lv.obj.FLAG.HIDDEN, hidden or week_view)
        slot["temp_min"].set_flag(lv.obj.FLAG.HIDDEN, hidden or not week_view)
        slot["temp_max"].set_flag(lv.obj.FLAG.HIDDEN, hidden or not week_view)
    except Exception:
        pass


def _hide_extra_slots(used_count, week_view=False):
    _assert_main()
    for i, slot in enumerate(forecast_slots):
        _set_slot_hidden(slot, i >= used_count, week_view=week_view)


def _render_today():
    _assert_main()
    buckets = forecast_mod.today_buckets(_forecast_data, max_slots=FORECAST_SLOTS)
    if not buckets:
        forecast_status.set_text("No forecast data yet")
        _hide_extra_slots(0, week_view=False)
        return
    forecast_status.set_text("")
    for i, slot in enumerate(forecast_slots):
        if i < len(buckets):
            b = buckets[i]
            slot["top"].set_text("{:02d}:00".format(b["hour"]))
            _set_slot_icon(slot, b["icon"])
            slot["temp"].set_text(_format_temp(b["temp"]))
    _hide_extra_slots(len(buckets), week_view=False)


def _render_week():
    _assert_main()
    days = forecast_mod.week_days(_forecast_data, max_days=FORECAST_SLOTS)
    if not days:
        forecast_status.set_text("No forecast data yet")
        _hide_extra_slots(0, week_view=True)
        return
    forecast_status.set_text("")
    for i, slot in enumerate(forecast_slots):
        if i < len(days):
            d = days[i]
            slot["top"].set_text(d["day_name"])
            _set_slot_icon(slot, d["icon"])
            slot["temp_min"].set_text(_format_temp(d["temp_min"]))
            slot["temp_max"].set_text(_format_temp(d["temp_max"]))
    _hide_extra_slots(len(days), week_view=True)


def _render_forecast():
    _assert_main()
    if _forecast_view == "week":
        forecast_title.set_text("Forecast - 5 days")
        forecast_subtitle.set_text("min / max")
        forecast_toggle_label_text("Show Today")
        _render_week()
    else:
        forecast_title.set_text("Forecast - Today")
        forecast_subtitle.set_text("next hours")
        forecast_toggle_label_text("Show Week")
        _render_today()


def forecast_toggle_label_text(text):
    """Set the toggle button label. Wraps a few API differences across m5ui
    versions (some expose set_text on the button itself, others on a child)."""
    _assert_main()
    try:
        forecast_toggle_btn.set_text(text)
    except AttributeError:
        try:
            forecast_toggle_btn.set_label_text(text)
        except Exception:
            pass


def _on_forecast_toggle(event_struct):
    # LVGL event callback — already on the main thread.
    global _forecast_view
    _forecast_view = "week" if _forecast_view == "today" else "today"
    _render_forecast()


def forecast_is_fetching():
    return _forecast_fetching


def set_forecast_fetching(val):
    global _forecast_fetching
    _forecast_fetching = bool(val)


# ----------------------------------------------------------------------------
# Navigation
# ----------------------------------------------------------------------------

def refresh_page():
    _assert_main()
    _ui_log("refresh_page ->", current_page)
    if current_page == 0:
        page0.screen_load()
    elif current_page == 1:
        page1.screen_load()
        # Re-render with whatever data we currently have so the page never
        # looks blank when the user lands on it after a refresh elsewhere.
        if _forecast_data is not None:
            _render_forecast()
    elif current_page == 2:
        page2.screen_load()
    elif current_page == 3:
        page3.screen_load()


def go_next_page():
    _assert_main()
    global current_page
    # Btn-driven nav: dashboard (0) -> forecast (1) -> voice (2) -> config (3).
    if current_page >= 3:
        return
    prev = current_page
    current_page += 1
    refresh_page()
    if _page_change_hook is not None:
        try:
            _page_change_hook(prev, current_page)
        except Exception as e:
            _ui_log("page hook error:", e)


def go_prev_page():
    _assert_main()
    global current_page
    if current_page <= 0:
        return
    prev = current_page
    current_page -= 1
    refresh_page()
    if _page_change_hook is not None:
        try:
            _page_change_hook(prev, current_page)
        except Exception as e:
            _ui_log("page hook error:", e)


def go_to_page(n):
    """Programmatic navigation, used by main.py to jump to page 3 on boot
    when STA fails. Bypasses the voice_client busy check because it's
    invoked outside human nav."""
    _assert_main()
    global current_page
    if not 0 <= n <= 3 or n == current_page:
        return
    prev = current_page
    current_page = n
    refresh_page()
    if _page_change_hook is not None:
        try:
            _page_change_hook(prev, current_page)
        except Exception as e:
            _ui_log("page hook error:", e)


def get_current_page():
    return current_page


def is_dashboard_active():
    """True when page 0 is on screen — used by main.py to skip work whose
    only output is dashboard widgets (clock, sensor labels, Wi-Fi icon)."""
    return current_page == 0


# ----------------------------------------------------------------------------
# Event handlers
# ----------------------------------------------------------------------------

def _on_voice_button_event(event_struct):
    # LVGL event callback — already on the main thread.
    event = event_struct.code
    if event == lv.EVENT.PRESSED:
        if voice_client.is_busy():
            return
        voice_client.start_recording()
    elif event == lv.EVENT.RELEASED:
        voice_client.stop_and_send()


# Face circle colours per assistant state.
# add_flag / clear_flag are unavailable on raw lv.obj in this UIFlow build,
# so expression is conveyed purely through background colour — no child widgets.
_FACE_STATES = {
    "idle":      0xF9D87A,   # yellow  — ready
    "recording": 0xFF6B6B,   # red     — listening
    "thinking":  0x74B9FF,   # blue    — waiting for server
    "speaking":  0x55EFC4,   # green   — playing reply
    "error":     0xFF7675,   # pink    — something went wrong
}


def _set_face_state(state):
    """Swap the face circle's background colour to reflect the current state."""
    _assert_main()
    if voice_btn_rec is None:
        return
    colour = _FACE_STATES.get(state, _FACE_STATES["idle"])
    try:
        voice_btn_rec.set_style_bg_color(lv.color_hex(colour), 0)
    except Exception as e:
        _ui_log("face state err:", e)

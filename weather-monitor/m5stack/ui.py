import time
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
temp_int_label = None
temp_dec_label = None
hum_val_label = None
co2_label = None
comfort_arrow = None
ampm_label = None
time_label = None
date_label = None
wifi_ind = None
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
voice_btn_rec = None
voice_label_status = None
voice_label_reply = None
voice_spinner = None

# Page 3 (Wi-Fi configuration) widgets
cfg_status_label = None
cfg_qr = None
cfg_qr_fallback_label = None
cfg_ssid_label = None
cfg_url_label = None


def _ui_log(*args):
    if DEBUG_UI:
        print("[ui]", *args)




def safe_font(size):
    name = "font_montserrat_{}".format(size)
    return getattr(lv, name, lv.font_montserrat_14)


def _make_image(parent, src, x, y):
    """Best-effort image factory. m5ui exposes M5Image on most firmware
    builds; older ones only have raw lv.image. Returns the LVGL object on
    success, None on failure (file missing / decoder unavailable)."""
    try:
        if hasattr(m5ui, "M5Image"):
            img = m5ui.M5Image(src, x=x, y=y, parent=parent)
            return img
        # Fallback to raw LVGL.
        ImgCls = getattr(lv, "image", None) or getattr(lv, "img", None)
        if ImgCls is None:
            return None
        img = ImgCls(parent)
        img.set_src(src)
        img.set_pos(x, y)
        return img
    except Exception as e:
        _ui_log("M5Image failed for", src, "->", e)
        return None


# ----------------------------------------------------------------------------
# Page builders
# ----------------------------------------------------------------------------

def _build_dashboard_page(font_large, font_small, font_tiny, text_color, bg_color):
    global page0
    global temp_int_label, temp_dec_label, hum_val_label, co2_label
    global comfort_arrow, ampm_label, time_label, date_label, wifi_ind, location_label

    page0 = m5ui.M5Page(bg_c=bg_color)

    temp_int_label = m5ui.M5Label("--", x=20, y=20, text_c=text_color, bg_c=bg_color, bg_opa=0, font=font_large, parent=page0)
    temp_dec_label = m5ui.M5Label(".0 °C", x=60, y=20, text_c=text_color, bg_opa=0, font=font_large, parent=page0)
    co2_label = m5ui.M5Label("CO2: -- ppm", x=80, y=8, text_c=text_color, bg_opa=0, font=font_small, parent=page0)
    hum_val_label = m5ui.M5Label("--", x=240, y=20, text_c=text_color, bg_opa=0, font=font_large, parent=page0)
    m5ui.M5Label("%", x=285, y=20, text_c=text_color, bg_opa=0, font=font_large, parent=page0)

    comfort_arrow = m5ui.M5Label(lv.SYMBOL.DOWN, x=155, y=75, text_c=text_color, bg_opa=0, font=font_small, parent=page0)

    bar_dry = m5ui.M5Label("", x=25, y=95, bg_c=0xF4A42D, bg_opa=255, parent=page0)
    bar_dry.set_size(90, 10)
    bar_comfort = m5ui.M5Label("", x=115, y=95, bg_c=0x27AE60, bg_opa=255, parent=page0)
    bar_comfort.set_size(90, 10)
    bar_wet = m5ui.M5Label("", x=205, y=95, bg_c=0x2980B9, bg_opa=255, parent=page0)
    bar_wet.set_size(90, 10)

    m5ui.M5Label("DRY",     x=55,  y=115, text_c=text_color, bg_opa=0, font=font_tiny, parent=page0)
    m5ui.M5Label("COMFORT", x=130, y=115, text_c=text_color, bg_opa=0, font=font_tiny, parent=page0)
    m5ui.M5Label("WET",     x=240, y=115, text_c=text_color, bg_opa=0, font=font_tiny, parent=page0)

    divider = m5ui.M5Label("", x=25, y=140, bg_c=0x888888, bg_opa=255, parent=page0)
    divider.set_size(270, 2)

    ampm_label = m5ui.M5Label("AM",       x=25,  y=175, text_c=text_color, bg_opa=0, font=font_small, parent=page0)
    time_label = m5ui.M5Label("--:--:--", x=55,  y=165, text_c=text_color, bg_opa=0, font=font_large, parent=page0)
    date_label = m5ui.M5Label("-/--",     x=230, y=170, text_c=text_color, bg_opa=0, font=font_large, parent=page0)
    wifi_ind = m5ui.M5Label(lv.SYMBOL.WIFI, x=300, y=5, text_c=0xE74C3C, bg_opa=0, font=font_small, parent=page0)
    location_label = m5ui.M5Label("", x=130, y=218, text_c=text_color, bg_opa=0, font=font_small, parent=page0)


def _build_forecast_page(font_small, font_tiny, text_color, bg_color):
    """Forecast page: title at top, 5 slots in the middle (icon + label +
    temp), and a centered toggle button at the bottom that flips between
    'Today' (next 5 three-hour buckets) and 'Week' (5-day summary)."""
    global page1, forecast_title, forecast_subtitle
    global forecast_toggle_btn, forecast_toggle_label, forecast_status, forecast_slots

    page1 = m5ui.M5Page(bg_c=bg_color)

    forecast_title = m5ui.M5Label("Forecast — Today", x=12, y=8,
                                   text_c=text_color, bg_opa=0, font=font_small, parent=page1)
    forecast_subtitle = m5ui.M5Label("--", x=12, y=28,
                                      text_c=0x555555, bg_opa=0, font=font_tiny, parent=page1)

    # 5 evenly spaced columns across the 320 px width.
    forecast_slots = []
    slot_w = 64
    margin = (320 - slot_w * FORECAST_SLOTS) // 2  # ~0
    for i in range(FORECAST_SLOTS):
        x0 = margin + i * slot_w
        top = m5ui.M5Label("", x=x0 + 16, y=52,
                            text_c=text_color, bg_opa=0, font=font_tiny, parent=page1)
        # Icon placeholder; real source filled in by render functions.
        icon = _make_image(page1, forecast_mod.icon_path("01d"), x=x0 + 4, y=72)
        temp = m5ui.M5Label("--", x=x0 + 14, y=136,
                             text_c=text_color, bg_opa=0, font=font_tiny, parent=page1)
        forecast_slots.append({"top": top, "icon": icon, "temp": temp, "x0": x0})

    forecast_status = m5ui.M5Label("", x=12, y=170,
                                    text_c=0x555555, bg_opa=0, font=font_tiny, parent=page1)

    forecast_toggle_btn = m5ui.M5Button(text="Show Week", x=90, y=195, w=140, h=34,
                                         bg_c=0x2980B9, text_c=0xFFFFFF,
                                         font=font_tiny, parent=page1)
    forecast_toggle_btn.add_event_cb(_on_forecast_toggle, lv.EVENT.CLICKED, None)


def _build_voice_page(font_small, font_tiny, text_color, bg_color):
    global page2, voice_btn_rec, voice_label_status, voice_label_reply, voice_spinner

    page2 = m5ui.M5Page(bg_c=bg_color)
    m5ui.M5Label("Ask Assistant", x=12, y=8, text_c=text_color, bg_opa=0, font=font_small, parent=page2)

    voice_label_status = m5ui.M5Label("Ready", x=12, y=30, text_c=text_color, bg_opa=0, font=font_tiny, parent=page2)
    voice_label_status.set_size(300, 18)

    voice_label_reply = m5ui.M5Label("", x=12, y=52, text_c=text_color, bg_opa=0, font=font_tiny, parent=page2)
    voice_label_reply.set_size(300, 100)

    if hasattr(m5ui, "M5Spinner"):
        voice_spinner = m5ui.M5Spinner(x=110, y=65, w=100, h=100, anim_t=10000, angle=180,
                                        bg_c=0xE7E3E7, bg_c_indicator=0x2193F3, parent=page2)
        voice_spinner.set_flag(lv.obj.FLAG.HIDDEN, True)
    else:
        voice_spinner = None

    voice_btn_rec = m5ui.M5Button(text="HOLD TO ASK", x=60, y=170, w=200, h=60,
                                   bg_c=0xC0392B, text_c=0xFFFFFF, font=font_small, parent=page2)
    voice_btn_rec.add_event_cb(_on_voice_button_event, lv.EVENT.ALL, None)

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
    global _wlan_sta
    _wlan_sta = wlan_sta

    font_large = safe_font(20)
    font_small = lv.font_montserrat_14
    font_tiny = safe_font(12)

    try:
        font_big = lv.binfont_create("S:/flash/res/font/roboto_40.bin")
        if font_big is None:
            raise OSError("binfont_create returned None")
    except Exception as e:
        print("Custom font load failed, falling back:", e)
        font_big = font_large
    globals()["_font_big_ref"] = font_big

    text_color = 0x000000
    bg_color = 0xD1D1D1

    _build_dashboard_page(font_big, font_small, font_tiny, text_color, bg_color)
    _build_forecast_page(font_small, font_tiny, text_color, bg_color)
    _build_voice_page(font_small, font_tiny, text_color, bg_color)
    _build_config_page(font_small, font_tiny, text_color, bg_color)

    page0.screen_load()


# ----------------------------------------------------------------------------
# Page-0 setters (called from main.py)
# ----------------------------------------------------------------------------

def set_temperature(temperature):
    temp_int = int(temperature)
    temp_dec = int(abs(temperature - temp_int) * 10)
    temp_int_label.set_text(str(temp_int))
    offset_x = 30 + (20 if temp_int < 10 else 35)
    temp_dec_label.set_pos(offset_x, 20)
    temp_dec_label.set_text(".{}°C".format(temp_dec))


def set_humidity(humidity):
    hum_val_label.set_text(str(int(humidity)))
    clamped = max(0, min(100, humidity))
    arrow_x = 25 + int((clamped / 100.0) * 270)
    comfort_arrow.set_pos(arrow_x - 5, 75)


def set_co2(co2):
    co2_label.set_text("CO2: {} ppm".format(co2))


def set_location(location_str):
    if not location_str:
        return
    x = max(0, 160 - (len(location_str) * 7) // 2)
    location_label.set_pos(x, 218)
    location_label.set_text(location_str)


def update_clock():
    t = time.localtime()
    hour, minute, second, month, day = t[3], t[4], t[5], t[1], t[2]
    h12 = hour % 12 or 12
    ampm_label.set_text("AM" if hour < 12 else "PM")
    time_label.set_text("{:02d}:{:02d}:{:02d}".format(h12, minute, second))
    date_label.set_text("{}/{}".format(month, day))


def refresh_wifi_indicator():
    """Recolour the dashboard Wi-Fi icon based on the STA association.
    Status text on the config page is driven by WifiManager callbacks
    (set_config_status_*) — this function only owns the icon."""
    try:
        if _wlan_sta is not None and _wlan_sta.isconnected():
            wifi_ind.set_text_color(0x2ECC71, 255, lv.PART.MAIN)
        else:
            wifi_ind.set_text_color(0xE74C3C, 255, lv.PART.MAIN)
    except Exception as e:
        _ui_log("refresh_wifi_indicator error:", e)


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
    """Replace the slot's icon by setting its src. The widget is the same
    instance so layout stays stable; only the bitmap source changes."""
    try:
        if slot["icon"] is None:
            slot["icon"] = _make_image(page1, forecast_mod.icon_path(code),
                                        x=slot["x0"] + 4, y=72)
            return
        slot["icon"].set_src(forecast_mod.icon_path(code))
    except Exception as e:
        _ui_log("set_slot_icon err:", e)


def _hide_extra_slots(used_count):
    for i, slot in enumerate(forecast_slots):
        hidden = i >= used_count
        try:
            slot["top"].set_flag(lv.obj.FLAG.HIDDEN, hidden)
            if slot["icon"] is not None:
                slot["icon"].set_flag(lv.obj.FLAG.HIDDEN, hidden)
            slot["temp"].set_flag(lv.obj.FLAG.HIDDEN, hidden)
        except Exception:
            pass


def _render_today():
    buckets = forecast_mod.today_buckets(_forecast_data, max_slots=FORECAST_SLOTS)
    if not buckets:
        forecast_status.set_text("No forecast data yet")
        _hide_extra_slots(0)
        return
    forecast_status.set_text("")
    for i, slot in enumerate(forecast_slots):
        if i < len(buckets):
            b = buckets[i]
            slot["top"].set_text("{:02d}:00".format(b["hour"]))
            _set_slot_icon(slot, b["icon"])
            slot["temp"].set_text(_format_temp(b["temp"]))
    _hide_extra_slots(len(buckets))


def _render_week():
    days = forecast_mod.week_days(_forecast_data, max_days=FORECAST_SLOTS)
    if not days:
        forecast_status.set_text("No forecast data yet")
        _hide_extra_slots(0)
        return
    forecast_status.set_text("")
    for i, slot in enumerate(forecast_slots):
        if i < len(days):
            d = days[i]
            slot["top"].set_text(d["day_name"])
            _set_slot_icon(slot, d["icon"])
            slot["temp"].set_text("{}/{}".format(
                _format_temp(d["temp_min"]), _format_temp(d["temp_max"])
            ))
    _hide_extra_slots(len(days))


def _render_forecast():
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
    try:
        forecast_toggle_btn.set_text(text)
    except AttributeError:
        try:
            forecast_toggle_btn.set_label_text(text)
        except Exception:
            pass


def _on_forecast_toggle(event_struct):
    global _forecast_view
    _forecast_view = "week" if _forecast_view == "today" else "today"
    _render_forecast()


def update_forecast(data):
    """Called from main.py after a successful forecast.fetch(). Stores the
    raw data and re-renders if the user is currently looking at this page."""
    global _forecast_data
    _forecast_data = data
    if data is None:
        forecast_status.set_text("Forecast unavailable")
        return
    if is_forecast_active():
        _render_forecast()


def forecast_show_loading():
    forecast_status.set_text("Loading forecast...")


def forecast_is_fetching():
    return _forecast_fetching


def set_forecast_fetching(val):
    global _forecast_fetching
    _forecast_fetching = bool(val)


# ----------------------------------------------------------------------------
# Navigation
# ----------------------------------------------------------------------------

def refresh_page():
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
    global current_page
    # Btn-driven nav: dashboard (0) -> forecast (1) -> voice (2) -> config (3).
    if current_page >= 3:
        return
    if voice_client.is_busy():
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
    global current_page
    if current_page <= 0:
        return
    if voice_client.is_busy():
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
# Configuration page setters (called from main._on_wifi_event)
# ----------------------------------------------------------------------------

def set_config_status_ap(info):
    """Render the AP-mode UI: red status, QR + visible AP credentials."""
    try:
        cfg_status_label.set_text("AP mode - connect to set up Wi-Fi")
        cfg_status_label.set_text_color(0xC0392B, 255, lv.PART.MAIN)
        _set_qr(info["url"])
        cfg_ssid_label.set_text("Wi-Fi: " + info.get("essid", ""))
        cfg_url_label.set_text(info.get("url", ""))
    except Exception as e:
        _ui_log("set_config_status_ap error:", e)


def set_config_status_connected(ssid, ip, url):
    """Render the connected-mode UI: green status, QR points to the device IP
    so the same web form is reachable from a phone on the same LAN."""
    try:
        cfg_status_label.set_text("Connected - " + ssid)
        cfg_status_label.set_text_color(0x27AE60, 255, lv.PART.MAIN)
        _set_qr(url)
        cfg_ssid_label.set_text("IP: " + ip)
        cfg_url_label.set_text(url)
    except Exception as e:
        _ui_log("set_config_status_connected error:", e)


def set_config_status_disconnected():
    try:
        cfg_status_label.set_text("Disconnected - searching...")
        cfg_status_label.set_text_color(0xE67E22, 255, lv.PART.MAIN)
    except Exception as e:
        _ui_log("set_config_status_disconnected error:", e)


# ----------------------------------------------------------------------------
# Event handlers
# ----------------------------------------------------------------------------

def _on_voice_button_event(event_struct):
    event = event_struct.code

    if event == lv.EVENT.PRESSED:
        if voice_client.is_busy():
            return
        try:
            voice_btn_rec.set_bg_color(0x6B0F0A, 255, 0)
        except Exception:
            pass
        voice_client.start_recording()

    elif event == lv.EVENT.RELEASED:
        try:
            voice_btn_rec.set_bg_color(0xC0392B, 255, 0)
        except Exception:
            pass
        voice_client.stop_and_send()


def _set_voice_status(text):
    try:
        voice_label_status.set_text(text)
    except Exception as e:
        _ui_log("voice status err:", e)


def _set_voice_reply(text):
    try:
        chunks = [text[i:i + 40] for i in range(0, len(text), 40)] or [""]
        voice_label_reply.set_text("\n".join(chunks[:5]))
    except Exception as e:
        _ui_log("voice reply err:", e)


def _show_voice_spinner(visible):
    try:
        if voice_spinner is not None:
            voice_spinner.set_flag(lv.obj.FLAG.HIDDEN, not visible)
    except Exception as e:
        _ui_log("voice spinner err:", e)

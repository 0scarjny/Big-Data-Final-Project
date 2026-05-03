import sys
import time
import _thread
import lvgl as lv
import m5ui

import wifi_manager
import voice_client

DEBUG_UI = True

_wlan_sta = None

# Pages (0-indexed):
#   0: dashboard, 1: voice assistant, 2: Wi-Fi list, 3: password keyboard
page0 = None
page1 = None
page2 = None
page3 = None
current_page = 0

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

# Page 1 (voice) widgets
voice_btn_rec = None
voice_label_status = None
voice_label_reply = None
voice_spinner = None

# Page 2 (Wi-Fi list) widgets
wifi_status_label = None
wifi_list = None
wifi_spinner = None

# Page 3 (password) widgets
pwd_ssid_label = None
pwd_textarea = None
pwd_status_label = None

_pending_ssid = None
_network_click_handlers = []
is_scanning = False
is_connecting = False


def _ui_log(*args):
    if DEBUG_UI:
        print("[ui]", *args)


def safe_font(size):
    name = "font_montserrat_{}".format(size)
    return getattr(lv, name, lv.font_montserrat_14)


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


def _build_voice_page(font_small, font_tiny, text_color, bg_color):
    global page1, voice_btn_rec, voice_label_status, voice_label_reply, voice_spinner

    page1 = m5ui.M5Page(bg_c=bg_color)
    m5ui.M5Label("Ask Assistant", x=12, y=8, text_c=text_color, bg_opa=0, font=font_small, parent=page1)

    voice_label_status = m5ui.M5Label("Ready", x=12, y=30, text_c=text_color, bg_opa=0, font=font_tiny, parent=page1)
    voice_label_status.set_size(300, 18)

    voice_label_reply = m5ui.M5Label("", x=12, y=52, text_c=text_color, bg_opa=0, font=font_tiny, parent=page1)
    voice_label_reply.set_size(300, 100)

    if hasattr(m5ui, "M5Spinner"):
        voice_spinner = m5ui.M5Spinner(x=110, y=65, w=100, h=100, anim_t=10000, angle=180,
                                        bg_c=0xE7E3E7, bg_c_indicator=0x2193F3, parent=page1)
        voice_spinner.set_flag(lv.obj.FLAG.HIDDEN, True)
    else:
        voice_spinner = None

    voice_btn_rec = m5ui.M5Button(text="HOLD TO ASK", x=60, y=170, w=200, h=60,
                                   bg_c=0xC0392B, text_c=0xFFFFFF, font=font_small, parent=page1)
    voice_btn_rec.add_event_cb(_on_voice_button_event, lv.EVENT.ALL, None)

    voice_client.prepare()
    voice_client.register_callbacks(_set_voice_status, _set_voice_reply, _show_voice_spinner)


def _build_wifi_page(font_small, font_tiny, text_color, bg_color):
    global page2, wifi_status_label, wifi_list, wifi_spinner

    page2 = m5ui.M5Page(bg_c=bg_color)
    m5ui.M5Label("Wi-Fi", x=12, y=8, text_c=text_color, bg_opa=0, font=font_small, parent=page2)

    wifi_status_label = m5ui.M5Label("Status: ...", x=12, y=30, text_c=text_color, bg_opa=0, font=font_tiny, parent=page2)

    scan_btn = m5ui.M5Button(text="Scan", x=210, y=6, w=45, h=28,
                             bg_c=0x2980B9, text_c=0xFFFFFF, font=font_tiny, parent=page2)
    scan_btn.add_event_cb(_on_scan_clicked, lv.EVENT.CLICKED, None)

    forget_btn = m5ui.M5Button(text="Forget", x=260, y=6, w=55, h=28,
                               bg_c=0xC0392B, text_c=0xFFFFFF, font=font_tiny, parent=page2)
    forget_btn.add_event_cb(_on_forget_clicked, lv.EVENT.CLICKED, None)

    wifi_list = m5ui.M5List(x=10, y=52, w=300, h=155, parent=page2)

    m5ui.M5Label("Tap a network to connect", x=12, y=215,
                 text_c=text_color, bg_opa=0, font=font_tiny, parent=page2)

    if hasattr(m5ui, "M5Spinner"):
        wifi_spinner = m5ui.M5Spinner(x=140, y=100, w=40, h=40, parent=page2)
        wifi_spinner.set_flag(lv.obj.FLAG.HIDDEN, True)
    else:
        wifi_spinner = None


def _build_password_page(font_small, font_tiny, text_color, bg_color):
    global page3, pwd_ssid_label, pwd_textarea, pwd_status_label

    page3 = m5ui.M5Page(bg_c=bg_color)

    pwd_ssid_label = m5ui.M5Label("", x=6, y=12, text_c=text_color, bg_opa=0, font=font_small, parent=page3)
    pwd_ssid_label.set_size(108, 24)

    pwd_textarea = m5ui.M5TextArea(x=120, y=4, w=196, h=32, placeholder="password", parent=page3)
    try:
        pwd_textarea.set_one_line(True)
        pwd_textarea.set_password_mode(False)
    except AttributeError:
        pass

    m5ui.M5Keyboard(x=0, y=40, w=320, h=170, target_textarea=pwd_textarea, parent=page3)

    cancel_btn = m5ui.M5Button(text="Cancel", x=4, y=212, w=96, h=26,
                               bg_c=0x7F8C8D, text_c=0xFFFFFF, font=font_tiny, parent=page3)
    cancel_btn.add_event_cb(_on_cancel_clicked, lv.EVENT.CLICKED, None)

    connect_btn = m5ui.M5Button(text="Connect", x=220, y=212, w=96, h=26,
                                bg_c=0x27AE60, text_c=0xFFFFFF, font=font_tiny, parent=page3)
    connect_btn.add_event_cb(_on_connect_clicked, lv.EVENT.CLICKED, None)

    pwd_status_label = m5ui.M5Label("", x=106, y=218, text_c=0xC0392B, bg_opa=0, font=font_tiny, parent=page3)



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
    _build_voice_page(font_small, font_tiny, text_color, bg_color)
    _build_wifi_page(font_small, font_tiny, text_color, bg_color)
    _build_password_page(font_small, font_tiny, text_color, bg_color)

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
    try:
        if _wlan_sta is not None and _wlan_sta.isconnected():
            ssid = wifi_manager.current_ssid(_wlan_sta) or "connected"
            wifi_ind.set_text_color(0x2ECC71, 255, lv.PART.MAIN)
            wifi_status_label.set_text("Status: connected to " + ssid)
        else:
            wifi_ind.set_text_color(0xE74C3C, 255, lv.PART.MAIN)
            wifi_status_label.set_text("Status: disconnected")
    except Exception as e:
        _ui_log("refresh_wifi_indicator error:", e)


# ----------------------------------------------------------------------------
# Navigation
# ----------------------------------------------------------------------------

def refresh_page():
    _ui_log("refresh_page ->", current_page)
    if current_page == 0:
        page0.screen_load()
    elif current_page == 1:
        page1.screen_load()
    elif current_page == 2:
        page2.screen_load()
    elif current_page == 3:
        page3.screen_load()


def go_next_page():
    global current_page
    # Btn-driven nav: dashboard (0) -> voice (1) -> wifi (2). Password (3)
    # is reached only by tapping a network on the wifi page.
    if current_page >= 2:
        return
    if voice_client.is_busy():
        # Don't yank the user off the voice page mid-recording / mid-reply.
        return
    current_page += 1
    refresh_page()
    if current_page == 2 and not _network_click_handlers:
        _ui_log("entered wifi page, auto-scanning")
        trigger_scan()


def go_prev_page():
    global current_page
    if current_page <= 0:
        return
    if voice_client.is_busy():
        return
    current_page -= 1
    refresh_page()


def get_current_page():
    return current_page


def _go_to_password_page(ssid):
    global current_page, _pending_ssid
    _ui_log("_go_to_password_page(", ssid, ")")
    _pending_ssid = ssid
    display = ssid if len(ssid) <= 11 else ssid[:10] + "…"
    pwd_ssid_label.set_text(display)
    saved = wifi_manager.load_credentials().get(ssid, "")
    pwd_textarea.set_text(saved)
    pwd_status_label.set_text("")
    current_page = 3
    refresh_page()


def _back_to_wifi_page():
    global current_page, _pending_ssid
    _pending_ssid = None
    pwd_textarea.set_text("")
    pwd_status_label.set_text("")
    current_page = 2
    refresh_page()


# ----------------------------------------------------------------------------
# Wi-Fi list
# ----------------------------------------------------------------------------

def _rssi_to_bars(rssi):
    if rssi >= -55: return "||||"
    if rssi >= -65: return "|||."
    if rssi >= -75: return "||.."
    if rssi >= -85: return "|..."
    return "...."


def _make_network_click_handler(ssid):
    # Closure binds ssid per-row. Avoids the LVGL-MicroPython gotcha where
    # event_struct.get_target_obj() returns a different Python wrapper than
    # M5List.add_button() did, so id()-based lookup fails.
    def handler(event_struct):
        _ui_log("network clicked:", ssid)
        _go_to_password_page(ssid)
    return handler


def _populate_wifi_list(networks):
    global _network_click_handlers

    try:
        wifi_list.clean()
    except AttributeError:
        child = wifi_list.get_child(0)
        while child is not None:
            child.delete()
            child = wifi_list.get_child(0)
    _network_click_handlers = []

    if not networks:
        wifi_list.add_text("No networks found. Tap Scan to retry.")
        return

    known = set(wifi_manager.known_ssids())
    for ssid, rssi, is_secure in networks:
        bars = _rssi_to_bars(rssi)
        lock = " " + lv.SYMBOL.EYE_CLOSE if is_secure else ""
        saved = " *" if ssid in known else ""
        label = "{}  {}{}{}".format(bars, ssid, lock, saved)
        btn = wifi_list.add_button(lv.SYMBOL.WIFI, text=label, h=34)
        handler = _make_network_click_handler(ssid)
        _network_click_handlers.append(handler)  # keep alive against GC
        btn.add_event_cb(handler, lv.EVENT.CLICKED, None)


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
        # crude line wrap so long replies stay on screen
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


def _on_scan_clicked(event_struct):
    trigger_scan()


def _on_forget_clicked(event_struct):
    ssid = wifi_manager.current_ssid(_wlan_sta)
    if not ssid:
        wifi_status_label.set_text("Status: nothing to forget")
        return
    wifi_manager.forget_credential(ssid)
    try:
        _wlan_sta.disconnect()
    except Exception as e:
        _ui_log("disconnect raised:", e)
    refresh_wifi_indicator()
    trigger_scan()


def _on_connect_clicked(event_struct):
    global is_connecting
    if is_connecting or _pending_ssid is None:
        return
    password = pwd_textarea.get_text() or ""
    pwd_status_label.set_text("Connecting...")
    pwd_status_label.set_text_color(0x555555, 255, lv.PART.MAIN)
    is_connecting = True
    _thread.start_new_thread(_connect_thread, (_pending_ssid, password))


def _on_cancel_clicked(event_struct):
    _back_to_wifi_page()


# ----------------------------------------------------------------------------
# Wi-Fi worker threads
# ----------------------------------------------------------------------------

def trigger_scan():
    global is_scanning
    if is_scanning:
        return
    is_scanning = True
    wifi_status_label.set_text("Status: scanning...")
    if wifi_spinner is not None:
        wifi_spinner.set_flag(lv.obj.FLAG.HIDDEN, False)
    _thread.start_new_thread(_scan_thread, ())


def _scan_thread():
    global is_scanning
    try:
        networks = wifi_manager.scan_networks(_wlan_sta)
        _populate_wifi_list(networks)
        wifi_status_label.set_text("Status: {} networks".format(len(networks)))
    except Exception as e:
        _ui_log("_scan_thread error:", type(e).__name__, e)
        try:
            sys.print_exception(e)
        except Exception:
            pass
        wifi_status_label.set_text("Status: scan error")
    finally:
        if wifi_spinner is not None:
            wifi_spinner.set_flag(lv.obj.FLAG.HIDDEN, True)
        is_scanning = False
        refresh_wifi_indicator()


def _connect_thread(ssid, password):
    global is_connecting
    try:
        ok = wifi_manager.connect_to(_wlan_sta, ssid, password, timeout_ms=15000)
        if ok:
            wifi_manager.add_credential(ssid, password)
            pwd_status_label.set_text("Connected")
            pwd_status_label.set_text_color(0x27AE60, 255, lv.PART.MAIN)
            time.sleep(1)
            _back_to_wifi_page()
        else:
            pwd_status_label.set_text("Failed. Check password.")
            pwd_status_label.set_text_color(0xC0392B, 255, lv.PART.MAIN)
    except Exception as e:
        _ui_log("_connect_thread error:", type(e).__name__, e)
        try:
            sys.print_exception(e)
        except Exception:
            pass
        pwd_status_label.set_text("Error: " + str(e)[:20])
        pwd_status_label.set_text_color(0xC0392B, 255, lv.PART.MAIN)
    finally:
        is_connecting = False
        refresh_wifi_indicator()


def start_boot_autoconnect():
    _thread.start_new_thread(_boot_autoconnect_thread, ())


def _boot_autoconnect_thread():
    try:
        wifi_manager.autoconnect(_wlan_sta, timeout_ms=10000)
    except Exception as e:
        _ui_log("_boot_autoconnect_thread error:", e)
    refresh_wifi_indicator()


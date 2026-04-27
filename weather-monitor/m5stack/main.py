import os, sys, io
import M5
from M5 import *
import m5ui
import lvgl as lv
import requests2
import network
import time
from machine import SoftI2C
from hardware import Pin, I2C
from unit import PIRUnit, ENVUnit, TVOCUnit
import hashlib
import binascii
import math
import asyncio
import _thread


import wifi_manager
from api_func import get_location_from_ip, IPDATA_KEY 


# --- UI and Sensor Globals ---
page0 = None
page1 = None
page2 = None
current_page = 0

# Page 0 elements
temp_int_label = None
temp_dec_label = None
hum_val_label = None
hum_pct_label = None
co2_label = None  # <-- Added CO2 label global
comfort_arrow = None
bar_dry = None
bar_comfort = None
bar_wet = None
lbl_dry = None
lbl_comfort = None
lbl_wet = None
divider_line = None
ampm_label = None
time_label = None
date_label = None
wifi_ind = None

# Page 1 (Wi-Fi menu) elements
wifi_title = None
wifi_status_label = None
wifi_scan_btn = None
wifi_forget_btn = None
wifi_list = None
wifi_hint_label = None
wifi_spinner = None

_list_button_ssids = {}
_network_click_handlers = []  # keeps closure callbacks alive (see _populate_wifi_list)

# Page 2 (password entry) elements
pwd_title = None
pwd_ssid_label = None
pwd_textarea = None
pwd_keyboard = None
pwd_connect_btn = None
pwd_cancel_btn = None
pwd_status_label = None
_pending_ssid = None

# Hardware & System
wlan_sta = None
i2c0 = None
env3_0 = None
tvoc_0 = None  # <-- Added tvoc_0 global
is_sending = False
humidity = None
temperature = None
co2 = None
h = None

is_scanning = False
is_connecting = False

DEBUG_UI = True


def _ui_log(*args):
    if DEBUG_UI:
        print("[ui]", *args)


def safe_font(size):
    font_name = "font_montserrat_{}".format(size)
    if hasattr(lv, font_name):
        return getattr(lv, font_name)
    return lv.font_montserrat_14


def init_hardware():
    global page0, page1, page2
    global temp_int_label, temp_dec_label, hum_val_label, hum_pct_label, co2_label
    global comfort_arrow, bar_dry, bar_comfort, bar_wet
    global lbl_dry, lbl_comfort, lbl_wet, divider_line
    global ampm_label, time_label, date_label, wifi_ind
    global wlan_sta, i2c0, env3_0, tvoc_0, h  # <-- Added tvoc_0 to globals

    M5.begin()
    Widgets.setRotation(1)
    m5ui.init()

    try:
        font_big_custom = lv.binfont_create("S:/flash/res/font/numbers_40.bin")
        if font_big_custom is None:
            raise OSError("binfont_create returned None")
    except Exception as e:
        print("Custom font load failed, falling back:", e)
        font_big_custom = safe_font(14)
    globals()["_font_big_custom_ref"] = font_big_custom

    font_large  = safe_font(20)
    font_medium = safe_font(20)
    font_small  = lv.font_montserrat_14
    font_tiny   = safe_font(12)

    text_color = 0x000000
    bg_color   = 0xD1D1D1

    # ================= PAGE 0: weather dashboard =================
    page0 = m5ui.M5Page(bg_c=bg_color)

    temp_int_label = m5ui.M5Label("--", x=20, y=20, text_c=text_color, bg_c=bg_color, bg_opa=0, font=font_large, parent=page0)
    temp_dec_label = m5ui.M5Label(".0 °C", x=60, y=20, text_c=text_color, bg_opa=0, font=font_small, parent=page0)
    
     # <-- Added CO2 label between the title and the buttons
    co2_label = m5ui.M5Label("CO2: -- ppm", x=80, y=8, text_c=text_color, bg_opa=0, font=font_small, parent=page0)

    hum_val_label = m5ui.M5Label("--", x=240, y=20, text_c=text_color, bg_opa=0, font=font_large, parent=page0)
    hum_pct_label = m5ui.M5Label("%", x=280, y=20, text_c=text_color, bg_opa=0, font=font_small, parent=page0)

    comfort_arrow = m5ui.M5Label(lv.SYMBOL.DOWN, x=155, y=75, text_c=text_color, bg_opa=0, font=font_small, parent=page0)

    bar_dry = m5ui.M5Label("", x=25, y=95, bg_c=0xF4A42D, bg_opa=255, parent=page0)
    bar_dry.set_size(90, 10)

    bar_comfort = m5ui.M5Label("", x=115, y=95, bg_c=0x27AE60, bg_opa=255, parent=page0)
    bar_comfort.set_size(90, 10)

    bar_wet = m5ui.M5Label("", x=205, y=95, bg_c=0x2980B9, bg_opa=255, parent=page0)
    bar_wet.set_size(90, 10)

    lbl_dry     = m5ui.M5Label("DRY",     x=55,  y=115, text_c=text_color, bg_opa=0, font=font_tiny, parent=page0)
    lbl_comfort = m5ui.M5Label("COMFORT", x=130, y=115, text_c=text_color, bg_opa=0, font=font_tiny, parent=page0)
    lbl_wet     = m5ui.M5Label("WET",     x=240, y=115, text_c=text_color, bg_opa=0, font=font_tiny, parent=page0)

    divider_line = m5ui.M5Label("", x=25, y=140, bg_c=0x888888, bg_opa=255, parent=page0)
    divider_line.set_size(270, 2)

    ampm_label = m5ui.M5Label("AM",        x=25,  y=175, text_c=text_color, bg_opa=0, font=font_small,  parent=page0)
    time_label = m5ui.M5Label("--:--:--",  x=55,  y=165, text_c=text_color, bg_opa=0, font=font_large,  parent=page0)
    date_label = m5ui.M5Label("-/--",      x=230, y=170, text_c=text_color, bg_opa=0, font=font_medium, parent=page0)
    wifi_ind   = m5ui.M5Label(lv.SYMBOL.WIFI, x=300, y=5, text_c=0xE74C3C, bg_opa=0, font=font_small,   parent=page0)

    # ================= PAGE 1: Wi-Fi menu =================
    _build_wifi_page(font_small, font_tiny, text_color, bg_color)

    # ================= PAGE 2: password entry =================
    _build_password_page(font_small, font_tiny, text_color, bg_color)

    # ================= HARDWARE INIT =================
    wlan_sta = network.WLAN(network.STA_IF)
    wlan_sta.active(True)
    _ui_log("WLAN STA active:", wlan_sta.active(),
            " connected:", wlan_sta.isconnected())

    passwd = "1234"
    h = hashlib.sha256(passwd)
    
    # ══ Hardware init ════════════════════════════════════════════════════════
    i2c0   = I2C(0, scl=Pin(33), sda=Pin(32), freq=100000)        # Port A
    i2c1   = SoftI2C(scl=Pin(13), sda=Pin(14), freq=100000)       # Port C (soft)
    #pir_0  = PIRUnit((36, 26))                                   # Port B
    env3_0 = ENVUnit(i2c=i2c0, type=3)
    tvoc_0 = TVOCUnit(i2c1) # <-- Now correctly assigned to the global variable
    

    time.timezone('GMT+2¨')

    _thread.stack_size(16384)
    _thread.start_new_thread(_boot_autoconnect_thread, ())

    page0.screen_load()


# ----------------------------------------------------------------------------
# Page 1 (Wi-Fi menu)
# ----------------------------------------------------------------------------

def _build_wifi_page(font_small, font_tiny, text_color, bg_color):
    global page1, wifi_title, wifi_status_label, wifi_scan_btn
    global wifi_forget_btn, wifi_list, wifi_hint_label, wifi_spinner
    

    page1 = m5ui.M5Page(bg_c=bg_color)

    wifi_title = m5ui.M5Label("Wi-Fi", x=12, y=8, text_c=text_color,
                              bg_opa=0, font=font_small, parent=page1)


    wifi_status_label = m5ui.M5Label("Status: ...", x=12, y=30,
                                     text_c=text_color, bg_opa=0,
                                     font=font_tiny, parent=page1)

    wifi_scan_btn = m5ui.M5Button(text="Scan", x=210, y=6, w=45, h=28,
                                  bg_c=0x2980B9, text_c=0xFFFFFF,
                                  font=font_tiny, parent=page1)
    wifi_scan_btn.add_event_cb(_on_scan_clicked, lv.EVENT.CLICKED, None)

    wifi_forget_btn = m5ui.M5Button(text="Forget", x=260, y=6, w=55, h=28,
                                    bg_c=0xC0392B, text_c=0xFFFFFF,
                                    font=font_tiny, parent=page1)
    wifi_forget_btn.add_event_cb(_on_forget_clicked, lv.EVENT.CLICKED, None)

    wifi_list = m5ui.M5List(x=10, y=52, w=300, h=155, parent=page1)

    wifi_hint_label = m5ui.M5Label("Tap a network to connect",
                                   x=12, y=215, text_c=text_color,
                                   bg_opa=0, font=font_tiny, parent=page1)

    if hasattr(m5ui, "M5Spinner"):
        wifi_spinner = m5ui.M5Spinner(x=140, y=100, w=40, h=40, parent=page1)
        wifi_spinner.set_flag(lv.obj.FLAG.HIDDEN, True)
    else:
        wifi_spinner = None


def _make_network_click_handler(ssid):
    """Build a click callback bound to one SSID via closure.

    Using a closure (rather than a shared handler that looks up the button
    from the event) sidesteps an LVGL-MicroPython gotcha: the Python wrapper
    returned by M5List.add_button() is not the same Python object you get
    back from event_struct.get_target_obj(), so id()-based lookup fails.
    """
    def handler(event_struct):
        _ui_log("Network clicked (closure): ssid =", ssid)
        _go_to_password_page(ssid)
    return handler


def _populate_wifi_list(networks):
    global wifi_list, _list_button_ssids, _network_click_handlers
    _ui_log("_populate_wifi_list: got", len(networks), "networks")

    try:
        wifi_list.clean()
    except AttributeError:
        _ui_log("  M5List.clean() not available, manually deleting children")
        child = wifi_list.get_child(0)
        while child is not None:
            child.delete()
            child = wifi_list.get_child(0)
    _list_button_ssids = {}
    _network_click_handlers = []  # drop old closures so they can be GC'd

    if not networks:
        _ui_log("  populating empty-state message")
        wifi_list.add_text("No networks found. Tap Scan to retry.")
        return

    known = set(wifi_manager.known_ssids())
    _ui_log("  known SSIDs in storage:", known)

    for ssid, rssi, is_secure in networks:
        bars = _rssi_to_bars(rssi)
        lock = " " + lv.SYMBOL.EYE_CLOSE if is_secure else ""
        saved = " *" if ssid in known else ""
        label = "{}  {}{}{}".format(bars, ssid, lock, saved)
        _ui_log("  adding list button:", label)

        btn = wifi_list.add_button(lv.SYMBOL.WIFI, text=label, h=34)
        _list_button_ssids[id(btn)] = ssid  # kept for debugging only

        handler = _make_network_click_handler(ssid)
        _network_click_handlers.append(handler)  # prevent MicroPython GC
        btn.add_event_cb(handler, lv.EVENT.CLICKED, None)
    _ui_log("_populate_wifi_list: done, total rows=", len(networks))


def _rssi_to_bars(rssi):
    if rssi >= -55: return "||||"
    if rssi >= -65: return "|||."
    if rssi >= -75: return "||.."
    if rssi >= -85: return "|..."
    return "...."


# ----------------------------------------------------------------------------
# Page 2 (password entry)
# ----------------------------------------------------------------------------

def _build_password_page(font_small, font_tiny, text_color, bg_color):
    global page2, pwd_title, pwd_ssid_label
    global pwd_textarea, pwd_keyboard
    global pwd_connect_btn, pwd_cancel_btn, pwd_status_label

    page2 = m5ui.M5Page(bg_c=bg_color)

    # Top strip: SSID label (left) and password textarea (right) share one
    # row. Drops ~40 px of vertical space vs stacking them, which we give
    # back to the keyboard below for bigger touch targets.
    pwd_ssid_label = m5ui.M5Label("", x=6, y=12, text_c=text_color,
                                  bg_opa=0, font=font_small, parent=page2)
    # Bound the label's width so long SSIDs don't push into the textarea.
    pwd_ssid_label.set_size(108, 24)

    pwd_textarea = m5ui.M5TextArea(x=120, y=4, w=196, h=32,
                                   placeholder="password", parent=page2)
    try:
        pwd_textarea.set_one_line(True)
        pwd_textarea.set_password_mode(False)
    except AttributeError:
        pass

    # Full-width, tall keyboard. 170 px / 4 rows ≈ 42 px per key --
    # comfortable for fingertip typing on the Core2's capacitive screen.
    pwd_keyboard = m5ui.M5Keyboard(x=0, y=40, w=320, h=170,
                                   target_textarea=pwd_textarea, parent=page2)

    # Slim action row pinned to the bottom.
    pwd_cancel_btn = m5ui.M5Button(text="Cancel", x=4, y=212, w=96, h=26,
                                   bg_c=0x7F8C8D, text_c=0xFFFFFF,
                                   font=font_tiny, parent=page2)
    pwd_cancel_btn.add_event_cb(_on_cancel_clicked, lv.EVENT.CLICKED, None)

    pwd_connect_btn = m5ui.M5Button(text="Connect", x=220, y=212, w=96, h=26,
                                    bg_c=0x27AE60, text_c=0xFFFFFF,
                                    font=font_tiny, parent=page2)
    pwd_connect_btn.add_event_cb(_on_connect_clicked, lv.EVENT.CLICKED, None)

    pwd_status_label = m5ui.M5Label("", x=106, y=218, text_c=0xC0392B,
                                    bg_opa=0, font=font_tiny, parent=page2)

    # Old page-title label is no longer shown; keep the global for
    # compatibility with any code that might still reference it.
    pwd_title = None


# ----------------------------------------------------------------------------
# Navigation
# ----------------------------------------------------------------------------

def refresh_page():
    _ui_log("refresh_page -> page", current_page)
    if current_page == 0:
        page0.screen_load()
    elif current_page == 1:
        page1.screen_load()
    elif current_page == 2:
        page2.screen_load()


def _go_to_password_page(ssid):
    global current_page, _pending_ssid
    _ui_log("_go_to_password_page(", ssid, ")")
    _pending_ssid = ssid
    # Narrow SSID column: truncate display so it never overlaps the textarea.
    display = ssid if len(ssid) <= 11 else ssid[:10] + "…"
    pwd_ssid_label.set_text(display)
    saved_pwd = wifi_manager.load_credentials().get(ssid, "")
    pwd_textarea.set_text(saved_pwd)
    pwd_status_label.set_text("")
    current_page = 2
    refresh_page()


def _back_to_wifi_page():
    global current_page, _pending_ssid
    _ui_log("_back_to_wifi_page")
    _pending_ssid = None
    pwd_textarea.set_text("")
    pwd_status_label.set_text("")
    current_page = 1
    refresh_page()


# ----------------------------------------------------------------------------
# Event handlers
# ----------------------------------------------------------------------------

def _on_scan_clicked(event_struct):
    _ui_log("Scan button clicked, is_scanning=", is_scanning)
    trigger_scan()


def _on_forget_clicked(event_struct):
    ssid = wifi_manager.current_ssid(wlan_sta)
    _ui_log("Forget clicked, current ssid =", ssid)
    if not ssid:
        wifi_status_label.set_text("Status: nothing to forget")
        return
    wifi_manager.forget_credential(ssid)
    try:
        wlan_sta.disconnect()
    except Exception as e:
        _ui_log("disconnect raised:", e)
    _refresh_wifi_status()
    trigger_scan()


def _on_network_clicked(event_struct):
    btn = event_struct.get_target_obj()
    ssid = _list_button_ssids.get(id(btn))
    _ui_log("Network clicked, ssid=", ssid)
    if ssid is None:
        _ui_log("  (id not found in _list_button_ssids, keys=", list(_list_button_ssids.keys()), ")")
        return
    _go_to_password_page(ssid)


def _on_connect_clicked(event_struct):
    global is_connecting
    _ui_log("Connect clicked, pending=", _pending_ssid, " is_connecting=", is_connecting)
    if is_connecting or _pending_ssid is None:
        return
    password = pwd_textarea.get_text() or ""
    ssid = _pending_ssid
    pwd_status_label.set_text("Connecting...")
    pwd_status_label.set_text_color(0x555555, 255, lv.PART.MAIN)
    is_connecting = True
    _thread.stack_size(16384)
    _thread.start_new_thread(_connect_thread, (ssid, password))


def _on_cancel_clicked(event_struct):
    _ui_log("Cancel clicked")
    _back_to_wifi_page()


# ----------------------------------------------------------------------------
# Background workers
# ----------------------------------------------------------------------------

def trigger_scan():
    global is_scanning
    _ui_log("trigger_scan, is_scanning=", is_scanning)
    if is_scanning:
        return
    is_scanning = True
    wifi_status_label.set_text("Status: scanning...")
    if wifi_spinner is not None:
        wifi_spinner.set_flag(lv.obj.FLAG.HIDDEN, False)
    _thread.stack_size(16384)
    _thread.start_new_thread(_scan_thread, ())


def _scan_thread():
    global is_scanning
    _ui_log("_scan_thread: enter")
    try:
        networks = wifi_manager.scan_networks(wlan_sta)
        _ui_log("_scan_thread: scan returned", len(networks), "networks")
        _populate_wifi_list(networks)
        wifi_status_label.set_text("Status: {} networks".format(len(networks)))
    except Exception as e:
        _ui_log("_scan_thread: exception:", type(e).__name__, e)
        try:
            sys.print_exception(e)
        except Exception:
            pass
        wifi_status_label.set_text("Status: scan error")
    finally:
        if wifi_spinner is not None:
            wifi_spinner.set_flag(lv.obj.FLAG.HIDDEN, True)
        is_scanning = False
        _refresh_wifi_status()
        _ui_log("_scan_thread: exit")


def _connect_thread(ssid, password):
    global is_connecting
    _ui_log("_connect_thread: enter ssid=", ssid)
    try:
        ok = wifi_manager.connect_to(wlan_sta, ssid, password, timeout_ms=15000)
        _ui_log("_connect_thread: connect_to returned", ok)
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
        _ui_log("_connect_thread: exception:", type(e).__name__, e)
        try:
            sys.print_exception(e)
        except Exception:
            pass
        pwd_status_label.set_text("Error: " + str(e)[:20])
        pwd_status_label.set_text_color(0xC0392B, 255, lv.PART.MAIN)
    finally:
        is_connecting = False
        _refresh_wifi_status()
        _ui_log("_connect_thread: exit")


def _boot_autoconnect_thread():
    _ui_log("_boot_autoconnect_thread: enter")
    try:
        wifi_manager.autoconnect(wlan_sta, timeout_ms=10000)
    except Exception as e:
        _ui_log("_boot_autoconnect_thread: exception:", e)
    _refresh_wifi_status()
    _ui_log("_boot_autoconnect_thread: exit")


def _refresh_wifi_status():
    try:
        if wlan_sta is not None and wlan_sta.isconnected():
            ssid = wifi_manager.current_ssid(wlan_sta) or "connected"
            wifi_ind.set_text_color(0x2ECC71, 255, lv.PART.MAIN)
            wifi_status_label.set_text("Status: connected to " + ssid)
        else:
            wifi_ind.set_text_color(0xE74C3C, 255, lv.PART.MAIN)
            wifi_status_label.set_text("Status: disconnected")
    except Exception as e:
        _ui_log("_refresh_wifi_status error:", e)


# ----------------------------------------------------------------------------
# Clock + sensor + cloud
# ----------------------------------------------------------------------------

def update_clock():
    t = time.localtime()
    year, month, day, hour, minute, second = t[0], t[1], t[2], t[3], t[4], t[5]
    ampm = "AM" if hour < 12 else "PM"
    h12 = hour % 12
    if h12 == 0:
        h12 = 12
    ampm_label.set_text(ampm)
    time_label.set_text("{:02d}:{:02d}:{:02d}".format(h12, minute, second))
    date_label.set_text("{}/{}".format(month, day))


def read_sensor():
    global humidity, temperature, co2
    try:
        # ENV3 logic
        if env3_0 is not None:
            raw_hum = env3_0.read_humidity()
            raw_temp = env3_0.read_temperature()
            humidity = round(raw_hum, 1)
            temperature = round(raw_temp, 1)
            temp_int = int(temperature)
            temp_dec = int(abs(temperature - temp_int) * 10)
            temp_int_label.set_text(str(temp_int))
            offset_x = 20 + (20 if temp_int < 10 else 35)
            temp_dec_label.set_pos(offset_x, 20)
            temp_dec_label.set_text(".{} °C".format(temp_dec))
            hum_val_label.set_text(str(int(humidity)))
            clamped_hum = max(0, min(100, humidity))
            arrow_x = 25 + int((clamped_hum / 100.0) * 270)
            comfort_arrow.set_pos(arrow_x - 5, 75)
            
        # TVOC logic <-- Added
        if tvoc_0 is not None:
            _ui_log("tvoc", tvoc_0.co2eq())
            co2 = tvoc_0.co2eq()
            co2_label.set_text("CO2: {} ppm".format(str(co2)))

    except Exception as e:
        print("Sensor read error:", e)


def send_data_thread(temp_val, hum_val, co2_val):
    global is_sending
    if temp_val is None or hum_val is None or co2_val is None:
        is_sending = False
        return
    t = time.localtime()
    current_date = "{:04d}-{:02d}-{:02d}".format(t[0], t[1], t[2])
    current_time = "{:02d}:{:02d}:{:02d}".format(t[3], t[4], t[5])
    data = {
        "passwd": '03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4',
        "values": {
            "date": current_date,
            "time": current_time,
            "indoor_temp": float(temp_val),
            "indoor_humidity": float(hum_val),
            "indoor_co2": float(co2_val)
        },
    }
    try:
        http_req = requests2.post(
            'https://flask-app-868833155300.europe-west6.run.app/send-to-bigquery',
            json=data,
        )
        print("Network Status:", http_req.status_code)
        http_req.close()
    except Exception as e:
        print("Network error:", e)
    finally:
        is_sending = False


# ----------------------------------------------------------------------------
# Async tasks
# ----------------------------------------------------------------------------

async def ui_task():
    global current_page
    btnA_prev = False
    btnC_prev = False

    while True:
        M5.update()
        btnA_now = M5.BtnA.isPressed()
        btnC_now = M5.BtnC.isPressed()

        if current_page != 2:
            if btnA_now and not btnA_prev:
                if current_page > 0:
                    current_page -= 1
                    refresh_page()
            if btnC_now and not btnC_prev:
                if current_page < 1:
                    current_page += 1
                    refresh_page()
                    if current_page == 1 and not _list_button_ssids:
                        _ui_log("entered page 1 for first time, auto-scanning")
                        trigger_scan()

        btnA_prev = btnA_now
        btnC_prev = btnC_now
        await asyncio.sleep_ms(50)


async def clock_task():
    while True:
        update_clock()
        await asyncio.sleep(1)


async def sensor_task():
    while True:
        read_sensor()
        await asyncio.sleep(5)


async def network_task():
    global temperature, humidity, co2, is_sending
    while True:
        await asyncio.sleep(60)
        _refresh_wifi_status()

        if wlan_sta.isconnected():
            if not is_sending:
                is_sending = True
                _thread.stack_size(16384)
                _thread.start_new_thread(send_data_thread, (temperature, humidity, co2))
            else:
                print("Previous request pending, skipping.")
        else:
            print("Skipping send: No Wi-Fi")


async def main():
    init_hardware()
    asyncio.create_task(ui_task())
    asyncio.create_task(clock_task())
    asyncio.create_task(sensor_task())
    asyncio.create_task(network_task())
    while True:
        await asyncio.sleep(3600)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (Exception, KeyboardInterrupt) as e:
        try:
            m5ui.deinit()
            from utility import print_error_msg
            print_error_msg(e)
        except ImportError:
            print("please update to latest firmware")

import time
import asyncio
import _thread
import ntptime

import M5
from M5 import Widgets
import m5ui
from machine import SoftI2C
from hardware import Pin, I2C
from unit import ENVUnit, TVOCUnit

from wifimanager2 import WifiManager

import ui
import cloud
import led
import voice_client
import forecast
import device_settings

SEND_TIMEOUT_MS = 45_000  # watchdog: force-reset is_sending after this long
FORECAST_REFRESH_S = 30 * 60  # 30 min between forecast refreshes

wlan_sta = None
env3_0 = None
tvoc_0 = None

temperature = None
humidity = None
co2 = None
location_str = None

is_sending = False
send_started_ms = 0

_sensor_reading = False  # re-entrancy guard for the sensor worker thread

# WifiManager2 lifecycle state. _ap_mode_active is True only while the user
# is on the configuration page (page 3) — AP radio + HTTP server are torn
# down on every other page so the asyncio loop stays snappy.
_ap_mode_active = False
_location_fetch_started = False
_ntp_synced = False

_NTP_SERVERS = ('ntp.aliyun.com', 'jp.pool.ntp.org', 'pool.ntp.org')


def is_ap_mode():
    return _ap_mode_active


def _enter_config_mode():
    """Bring up the AP radio and re-enable the HTTP config server. Called
    from the UI page-change hook when the user navigates to page 3."""
    global _ap_mode_active
    if _ap_mode_active:
        return
    _ap_mode_active = True

    try:
        ap_cfg = WifiManager.ap_config["config"]
    except (AttributeError, KeyError, TypeError):
        ap_cfg = {"essid": "M5Core2_Setup", "password": "mypassword"}
    try:
        ap = WifiManager.accesspoint()
        ap.active(True)
        ap.config(**ap_cfg)
    except Exception as e:
        print("[main] AP enable failed:", e)

    # The config server task was spawned once at boot and idles when
    # _config_server_enabled is False — flipping it back to True makes the
    # task bind a fresh socket on its next tick.
    WifiManager._config_server_enabled = True

    info = {
        "essid": ap_cfg.get("essid", "M5Core2_Setup"),
        "password": ap_cfg.get("password", ""),
        "url": "http://192.168.4.1:8080",
    }
    ui.set_config_status_ap(info)


def _exit_config_mode():
    """Tear down the AP and pause the config server. Called from the page
    hook when leaving page 3."""
    global _ap_mode_active
    if not _ap_mode_active:
        return
    _ap_mode_active = False

    WifiManager.stop_config_server()  # flips _config_server_enabled = False
    try:
        WifiManager.accesspoint().active(False)
    except Exception as e:
        print("[main] AP disable failed:", e)

    # Refresh the config-page labels so they show the right STA state next
    # time the user returns.
    try:
        if WifiManager.wlan().isconnected():
            ip = WifiManager.wlan().ifconfig()[0]
            try:
                ssid = WifiManager.wlan().config('essid')
            except Exception:
                ssid = "?"
            ui.set_config_status_connected(ssid, ip, "http://{}:8080".format(ip))
        else:
            ui.set_config_status_disconnected()
    except Exception as e:
        print("[main] post-exit status update failed:", e)


def _on_page_change(prev, now):
    if now == 3 and prev != 3:
        _enter_config_mode()
    elif prev == 3 and now != 3:
        _exit_config_mode()


def _ntp_sync_thread():
    """Try each NTP server in order and set the RTC to UTC on first success."""
    global _ntp_synced
    for host in _NTP_SERVERS:
        try:
            ntptime.host = host
            ntptime.settime()  # sets RTC to UTC
            _ntp_synced = True
            print("[ntp] synced via", host)
            return
        except Exception as e:
            print("[ntp] {} failed: {}".format(host, e))
    print("[ntp] all servers failed, clock may be inaccurate")


def _on_wifi_event(event, **kw):
    """Connection-state callback from WifiManager. Drives:
      - the dashboard Wi-Fi icon colour (green when connected, red otherwise)
      - the one-shot NTP sync and location fetch on first STA association
    Page-3 status text is owned by _enter_config_mode/_exit_config_mode and
    isn't touched here."""
    global _location_fetch_started
    if event == "connected":
        ui.refresh_wifi_indicator()
        if not _ntp_synced:
            _thread.start_new_thread(_ntp_sync_thread, ())
        if not _location_fetch_started:
            _location_fetch_started = True
            override = device_settings.get('location_override')
            if override:
                _on_location_done(override)
            elif wlan_sta is not None:
                _thread.start_new_thread(cloud.fetch_location, (wlan_sta, _on_location_done))
    elif event in ("disconnected", "connection_failed"):
        ui.refresh_wifi_indicator()


def init_hardware():
    global wlan_sta, env3_0, tvoc_0

    M5.begin()
    Widgets.setRotation(1)
    m5ui.init()

    WifiManager.config_file = 'networks.json'
    WifiManager.on_connection_change(_on_wifi_event)
    ui.set_page_change_hook(_on_page_change)
    wlan_sta = WifiManager.wlan()
    wlan_sta.active(True)
    print("[main] WLAN STA active:", wlan_sta.active(), " connected:", wlan_sta.isconnected())

    i2c0 = I2C(0, scl=Pin(33), sda=Pin(32), freq=100000)         # Port A
    i2c1 = SoftI2C(scl=Pin(13), sda=Pin(14), freq=100000)        # Port C (soft)
    env3_0 = ENVUnit(i2c=i2c0, type=3)
    tvoc_0 = TVOCUnit(i2c1)

    led.init()  # RGB status indicator (no-op if hardware isn't present)
    led.set_enabled(device_settings.get('led_signals_enabled'))

    time.timezone(device_settings.get('timezone'))

    _thread.stack_size(16384)

    ui.init(wlan_sta)


def _on_location_done(loc):
    global location_str
    if loc:
        location_str = str(loc)
        ui.set_location(location_str)
        voice_client.set_location(location_str)


def _on_send_done():
    global is_sending
    is_sending = False


def read_sensor():
    """Sync I2C reads. Refreshes globals always (so the next cloud upload
    has fresh values) but only writes to LVGL widgets when the dashboard
    is actually visible — labels on hidden pages are wasted work and add
    contention for the LVGL renderer."""
    global temperature, humidity, co2
    try:
        if env3_0 is not None:
            humidity = round(env3_0.read_humidity(), 1)
            temperature = round(env3_0.read_temperature(), 1)
            if ui.is_dashboard_active():
                ui.set_temperature(temperature)
                ui.set_humidity(humidity)
        if tvoc_0 is not None:
            co2 = tvoc_0.co2eq()
            if ui.is_dashboard_active():
                ui.set_co2(co2)
    except Exception as e:
        print("Sensor read error:", e)


def _sensor_thread():
    """Worker that runs read_sensor off the asyncio loop so the ~50–100 ms
    I2C round-trip never blocks button polling."""
    global _sensor_reading
    try:
        read_sensor()
    finally:
        _sensor_reading = False


def _forecast_thread():
    """Worker thread for forecast.fetch(). HTTP must never block the asyncio
    loop, and the UI update is fast (in-memory dict + LVGL writes)."""
    try:
        data = forecast.fetch(location_str)
        ui.update_forecast(data)
    except Exception as e:
        print("[forecast] thread error:", e)
        ui.update_forecast(None)
    finally:
        ui.set_forecast_fetching(False)


# ----------------------------------------------------------------------------
# Async tasks
# ----------------------------------------------------------------------------

async def ui_task():
    btnA_prev = False
    btnC_prev = False
    while True:
        M5.update()
        btnA_now = M5.BtnA.isPressed()
        btnC_now = M5.BtnC.isPressed()

        if btnA_now and not btnA_prev:
            ui.go_prev_page()
        if btnC_now and not btnC_prev:
            ui.go_next_page()

        btnA_prev = btnA_now
        btnC_prev = btnC_now
        await asyncio.sleep_ms(50)


async def clock_task():
    while True:
        # Clock label only exists on the dashboard — skip the LVGL write
        # when the user is on another page.
        if ui.is_dashboard_active():
            ui.update_clock()
        await asyncio.sleep(1)


async def sensor_task():
    """Dispatches read_sensor to a worker thread so I2C never blocks the
    asyncio loop. Cadence is 5 s while the dashboard is visible (live UI)
    and 30 s otherwise — still always fresher than the per-minute cloud upload.

    Pauses entirely while in AP mode so the HTTP config server has the cycles
    it needs for socket IO and JSON parsing."""
    global _sensor_reading
    while True:
        if is_ap_mode():
            await asyncio.sleep(2)
            continue
        if not _sensor_reading:
            _sensor_reading = True
            _thread.start_new_thread(_sensor_thread, ())
        if ui.is_dashboard_active():
            await asyncio.sleep(5)
        else:
            await asyncio.sleep(30)


async def forecast_task():
    """Refresh the forecast every FORECAST_REFRESH_S. First fetch waits for
    Wi-Fi to come up. Each fetch runs in a worker thread so HTTP doesn't
    stall the asyncio loop.

    On boot, any cached forecast from the previous session is shown
    immediately so the page is never blank while waiting for the first
    HTTP round-trip to the backend."""
    # Show cached data from the previous boot immediately — the page is
    # usable right away while the fresh fetch runs in the background.
    cached = forecast.load_cache()
    if cached is not None:
        print("[forecast] loaded cache from flash")
        ui.update_forecast(cached)
    else:
        ui.forecast_show_loading()

    # Wait for Wi-Fi before the first live fetch.
    for _ in range(60):  # up to ~60 s
        if wlan_sta is not None and wlan_sta.isconnected():
            break
        await asyncio.sleep(1)

    while True:
        if is_ap_mode():
            await asyncio.sleep(2)
            continue
        if wlan_sta.isconnected() and not ui.forecast_is_fetching():
            ui.set_forecast_fetching(True)
            _thread.start_new_thread(_forecast_thread, ())
        await asyncio.sleep(FORECAST_REFRESH_S)


async def network_task():
    global is_sending, send_started_ms
    interval = device_settings.get('send_interval_s')
    while True:
        # Align to the next wall-clock interval boundary
        t = time.localtime()
        sec_of_day = t[3]*3600 + t[4]*60 + t[5]
        delay = interval - (sec_of_day % interval)
        if delay == 0:
            delay = interval
        await asyncio.sleep(delay)
        if is_ap_mode():
            continue
        # Wi-Fi icon only exists on the dashboard.
        if ui.is_dashboard_active():
            ui.refresh_wifi_indicator()

        # Watchdog: if a previous send is still in flight and has been for too
        # long, the worker is stuck (e.g. requests2 hung past its own timeout).
        # Force-reset so we can try again. Without this the device silently
        # stops sending forever — see the ~01:15 hang in the field log.
        if is_sending and time.ticks_diff(time.ticks_ms(), send_started_ms) > SEND_TIMEOUT_MS:
            print("Send watchdog: forcing reset")
            is_sending = False

        if not wlan_sta.isconnected():
            print("Skipping send: No Wi-Fi")
            continue

        if is_sending:
            print("Previous request pending, skipping.")
            continue

        is_sending = True
        send_started_ms = time.ticks_ms()
        _thread.start_new_thread(
            cloud.send_data,
            (temperature, humidity, co2, location_str, _on_send_done),
        )


async def wifi_keepalive_task():
    """Cheap STA reconnect watchdog. Replaces WifiManager.start_managing(),
    which retried on a tight 10 s cadence with full blocking scans. We poll
    every 60 s and only invoke setup_network() when STA is down AND we're
    not in config mode (where the user is editing networks)."""
    while True:
        await asyncio.sleep(60)
        if is_ap_mode():
            continue
        try:
            if not WifiManager.wlan().isconnected():
                print("[main] STA disconnected - reconnecting")
                WifiManager.setup_network()
                # Same defensive AP-off as in the boot path: setup_network()
                # may have brought the AP up per the on-device start_policy.
                try:
                    WifiManager.accesspoint().active(False)
                except Exception:
                    pass
        except Exception as e:
            print("[main] keepalive error:", e)


BOOT_WIFI_ATTEMPTS = 5
BOOT_WIFI_RETRY_DELAY_S = 2


async def main():
    init_hardware()

    # Boot connect: retry up to BOOT_WIFI_ATTEMPTS times before giving up. A
    # single setup_network() attempt has only a 5 s per-network association
    # window inside wifimanager2, which is often too short on cold boot
    # while the radio is still warming up. Trying a handful of times gives
    # the ESP32 a chance to actually associate before we fall back to AP.
    connected = False
    for attempt in range(1, BOOT_WIFI_ATTEMPTS + 1):
        print("[main] Wi-Fi connect attempt {}/{}".format(attempt, BOOT_WIFI_ATTEMPTS))
        # Tear down AP before every scan. setup_network() enables the AP
        # whenever STA connect fails (per start_policy), so without this the
        # AP stays active on every retry and its radio interferes with the STA
        # channel scan, causing the SSID to disappear from scan results.
        try:
            WifiManager.accesspoint().active(False)
        except Exception:
            pass
        try:
            if WifiManager.setup_network():
                connected = True
                print("[main] Wi-Fi connected on attempt", attempt)
                break
        except Exception as e:
            print("[main] setup_network raised:", e)
        if attempt < BOOT_WIFI_ATTEMPTS:
            await asyncio.sleep(BOOT_WIFI_RETRY_DELAY_S)

    # Defensive AP-off after the boot phase, run on BOTH success and failure
    # paths. Per the wifimanager2 README, start_policy:"always" brings the AP
    # up regardless of STA state and "fallback" brings it up between failed
    # retries — so on every code path, setup_network() may have left the AP
    # radio active. Tear it down here; _enter_config_mode() will bring it
    # back up only when the user navigates to page 3.
    try:
        WifiManager.accesspoint().active(False)
    except Exception as e:
        print("[main] post-boot AP disable failed:", e)

    # Spawn the config-server task in the *disabled* state. It loops forever
    # checking _config_server_enabled — _enter_config_mode flips it on, and
    # _exit_config_mode flips it off, so we never spawn a second task.
    WifiManager._config_server_enabled = False
    WifiManager.start_config_server("")
    WifiManager._config_server_enabled = False  # start_config_server resets it

    if not connected:
        print("[main] STA failed after {} attempts - entering config mode".format(BOOT_WIFI_ATTEMPTS))
        ui.go_to_page(3)

    asyncio.create_task(wifi_keepalive_task())
    asyncio.create_task(ui_task())
    asyncio.create_task(clock_task())
    asyncio.create_task(sensor_task())
    asyncio.create_task(network_task())
    asyncio.create_task(forecast_task())
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

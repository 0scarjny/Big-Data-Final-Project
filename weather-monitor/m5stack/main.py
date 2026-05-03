import time
import asyncio
import _thread
import network

import M5
from M5 import Widgets
import m5ui
from machine import SoftI2C
from hardware import Pin, I2C
from unit import ENVUnit, TVOCUnit

import ui
import cloud
import voice_client

SEND_INTERVAL_S = 60
SEND_TIMEOUT_MS = 45_000  # watchdog: force-reset is_sending after this long

wlan_sta = None
env3_0 = None
tvoc_0 = None

temperature = None
humidity = None
co2 = None
location_str = None

is_sending = False
send_started_ms = 0


def init_hardware():
    global wlan_sta, env3_0, tvoc_0

    M5.begin()
    Widgets.setRotation(1)
    m5ui.init()

    wlan_sta = network.WLAN(network.STA_IF)
    wlan_sta.active(True)
    print("[main] WLAN STA active:", wlan_sta.active(), " connected:", wlan_sta.isconnected())

    i2c0 = I2C(0, scl=Pin(33), sda=Pin(32), freq=100000)         # Port A
    i2c1 = SoftI2C(scl=Pin(13), sda=Pin(14), freq=100000)        # Port C (soft)
    env3_0 = ENVUnit(i2c=i2c0, type=3)
    tvoc_0 = TVOCUnit(i2c1)

    time.timezone('GMT+2')

    _thread.stack_size(16384)

    ui.init(wlan_sta)
    ui.start_boot_autoconnect()
    _thread.start_new_thread(cloud.fetch_location, (wlan_sta, _on_location_done))


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
    global temperature, humidity, co2
    try:
        if env3_0 is not None:
            humidity = round(env3_0.read_humidity(), 1)
            temperature = round(env3_0.read_temperature(), 1)
            ui.set_temperature(temperature)
            ui.set_humidity(humidity)
        if tvoc_0 is not None:
            co2 = tvoc_0.co2eq()
            ui.set_co2(co2)
    except Exception as e:
        print("Sensor read error:", e)


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

        # Page 3 is the password keyboard — block physical-button nav so it
        # doesn't yank the user away mid-typing.
        if ui.get_current_page() != 3:
            if btnA_now and not btnA_prev:
                ui.go_prev_page()
            if btnC_now and not btnC_prev:
                ui.go_next_page()

        btnA_prev = btnA_now
        btnC_prev = btnC_now
        await asyncio.sleep_ms(50)


async def clock_task():
    while True:
        ui.update_clock()
        await asyncio.sleep(1)


async def sensor_task():
    while True:
        read_sensor()
        await asyncio.sleep(5)


async def network_task():
    global is_sending, send_started_ms
    while True:
        await asyncio.sleep(SEND_INTERVAL_S)
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

import time
import json
import asyncio
import requests2

import led
from api_func import get_location_from_ip
from config import SHARED_SECRET, IPDATA_KEY, BIGQUERY_URL

HTTP_TIMEOUT_S = 10


async def send_data(temperature, humidity, co2, location):
    """POST a reading to the BigQuery proxy. Coroutine: the LED animation
    yields, the HTTP POST itself is blocking (no async HTTP available)."""
    if temperature is None or humidity is None or co2 is None:
        print("send_data: missing reading, skipping")
        return

    t = time.localtime()
    payload = {
        "passwd": SHARED_SECRET,
        "values": {
            "date": "{:04d}-{:02d}-{:02d}".format(t[0], t[1], t[2]),
            "time": "{:02d}:{:02d}:{:02d}".format(t[3], t[4], t[5]),
            "indoor_temp": float(temperature),
            "indoor_humidity": float(humidity),
            "indoor_co2": float(co2),
        },
        "location": location,
    }
    print("Prepared data for sending:", payload)

    json_str = json.dumps(payload)
    utf8_payload = json_str.encode('utf-8')
    headers = {"Content-Type": "application/json; charset=utf-8"}

    try:
        # 5 rapid green blinks signal "upload in progress" before we block
        # on the HTTP round-trip.
        await led.flash_sending()
        resp = requests2.post(
            BIGQUERY_URL,
            data=utf8_payload,
            headers=headers,
            timeout=HTTP_TIMEOUT_S,
        )
        status = resp.status_code
        print("Network Status:", status)
        resp.close()
        if status == 200:
            await led.flash_success()
        else:
            await led.flash_error()
    except Exception as e:
        print("Network error:", e)
        await led.flash_error()


async def fetch_location(wlan_sta):
    """Wait up to 30 s for Wi-Fi, then resolve location from public IP.
    Returns the location string (or None on failure / timeout)."""
    for _ in range(30):
        if wlan_sta is not None and wlan_sta.isconnected():
            await asyncio.sleep(1)
            try:
                location = get_location_from_ip(IPDATA_KEY)
                print("[cloud] location:", location)
                return location
            except Exception as e:
                print("[cloud] location error:", e)
                return None
        await asyncio.sleep(1)
    print("[cloud] location: timed out waiting for wifi")
    return None

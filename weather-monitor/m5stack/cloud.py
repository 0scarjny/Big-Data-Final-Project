import time
import json
import requests2

import led
from api_func import get_location_from_ip, IPDATA_KEY

BIGQUERY_URL = 'https://flask-app-868833155300.europe-west6.run.app/send-to-bigquery'
SHARED_SECRET = '03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4'
HTTP_TIMEOUT_S = 10


def send_data(temperature, humidity, co2, location, on_done):
    try:
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

        # 1. Convert the dictionary to a JSON string
        json_str = json.dumps(payload)
        
        # 2. Encode the string into UTF-8 bytes explicitly
        utf8_payload = json_str.encode('utf-8')
        
        # 3. Explicitly tell the server we are sending UTF-8 encoded JSON
        headers = {
            "Content-Type": "application/json; charset=utf-8"
        }

        try:
            # 5 rapid green blinks signal "upload in progress" before we
            # block on the HTTP round-trip.
            led.flash_sending()
            # 4. Use `data=utf8_payload` instead of `json=payload`
            resp = requests2.post(
                BIGQUERY_URL,
                data=utf8_payload,
                headers=headers,
                timeout=HTTP_TIMEOUT_S
            )
            status = resp.status_code
            print("Network Status:", status)
            resp.close()
            if status == 200:
                led.flash_success()
            else:
                led.flash_error()
        except Exception as e:
            print("Network error:", e)
            led.flash_error()
    finally:
        on_done()


def fetch_location(wlan_sta, on_done):
    location = None
    try:
        for _ in range(30):
            if wlan_sta is not None and wlan_sta.isconnected():
                time.sleep(1)
                try:
                    location = get_location_from_ip(IPDATA_KEY)
                    print("[cloud] location:", location)
                except Exception as e:
                    print("[cloud] location error:", e)
                break
            time.sleep(1)
        else:
            print("[cloud] location: timed out waiting for wifi")
    finally:
        on_done(location)

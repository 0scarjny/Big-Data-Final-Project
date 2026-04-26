import os

import requests
from flask import Flask, request

try:
    from src.clients import PASSWORD_HASH, WEATHER_TABLE_PATH, client, df, API_KEY
except ImportError:
    from clients import PASSWORD_HASH, WEATHER_TABLE_PATH, client, df, API_KEY

app = Flask(__name__)

OPENWEATHER_BASE = "https://api.openweathermap.org"
GEOCODE_URL = f"{OPENWEATHER_BASE}/geo/1.0/direct"
WEATHER_URL = f"{OPENWEATHER_BASE}/data/2.5/weather"
FORECAST_URL = f"{OPENWEATHER_BASE}/data/2.5/forecast"


def _require_auth(payload):
    if payload.get("passwd") != PASSWORD_HASH:
        return {"status": "failed", "error": "Incorrect Password!"}, 401
    return None


def _geocode(city):
    r = requests.get(
        GEOCODE_URL,
        params={"q": city, "limit": 1, "appid": API_KEY},
        timeout=10,
    )
    r.raise_for_status()
    results = r.json()
    if not results:
        return None
    return results[0]["lat"], results[0]["lon"]


@app.route("/send-to-bigquery", methods=["GET", "POST"])
def send_to_bigquery():
    if request.method == "POST":
        if request.get_json(force=True)["passwd"] != PASSWORD_HASH:
            raise Exception("Incorrect Password!")
        data = request.get_json(force=True)["values"]
        names, values = "", ""
        for k, v in data.items():
            names += f"{k},"
            if df.dtypes[k] == float:
                values += f"{v},"
            else:
                values += f"'{v}',"
        q = f"INSERT INTO `{WEATHER_TABLE_PATH}` ({names[:-1]}) VALUES({values[:-1]})"
        client.query(q).result()
        return {"status": "sucess", "data": data}
    return {"status": "failed"}


@app.route("/get_outdoor_weather", methods=["POST"])
def get_outdoor_weather():
    payload = request.get_json(force=True)
    auth_err = _require_auth(payload)
    if auth_err:
        return auth_err
    city = payload.get("city")
    if not city:
        return {"status": "failed", "error": "Missing 'city'"}, 400
    coords = _geocode(city)
    if coords is None:
        return {"status": "failed", "error": f"City not found: {city}"}, 404
    lat, lon = coords
    r = requests.get(
        WEATHER_URL,
        params={"lat": lat, "lon": lon, "appid": API_KEY, "units": "metric"},
        timeout=10,
    )
    r.raise_for_status()
    return {"status": "success", "city": city, "data": r.json()}


@app.route("/get_forecast", methods=["POST"])
def get_forecast():
    payload = request.get_json(force=True)
    auth_err = _require_auth(payload)
    if auth_err:
        return auth_err
    city = payload.get("city")
    if not city:
        return {"status": "failed", "error": "Missing 'city'"}, 400
    coords = _geocode(city)
    if coords is None:
        return {"status": "failed", "error": f"City not found: {city}"}, 404
    lat, lon = coords
    r = requests.get(
        FORECAST_URL,
        params={"lat": lat, "lon": lon, "appid": API_KEY, "units": "metric"},
        timeout=10,
    )
    r.raise_for_status()
    return {"status": "success", "city": city, "data": r.json()}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)

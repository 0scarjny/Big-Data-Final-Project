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


# @app.route("/send-to-bigquery", methods=["GET", "POST"])
# def send_to_bigquery():
#     if request.method == "POST":
#         if request.get_json(force=True)["passwd"] != PASSWORD_HASH:
#             raise Exception("Incorrect Password!")
#         data = request.get_json(force=True)["values"]
#         names, values = "", ""
#         for k, v in data.items():
#             names += f"{k},"
#             if df.dtypes[k] == float:
#                 values += f"{v},"
#             else:
#                 values += f"'{v}',"
#         q = f"INSERT INTO `{WEATHER_TABLE_PATH}` ({names[:-1]}) VALUES({values[:-1]})"
#         client.query(q).result()
#         return {"status": "sucess", "data": data}
#     return {"status": "failed"}

@app.route("/send-to-bigquery", methods=["GET", "POST"])
def send_to_bigquery():
    if request.method == "POST":
        # 1. Parse payload and authenticate
        payload = request.get_json(force=True)
        auth_err = _require_auth(payload)
        if auth_err:
            return auth_err

        # 2. Extract local IoT data and location context
        local_values = payload.get("values", {})
        location = payload.get("location", "Lausanne")  # Default city if not provided

        try:
            # 3. Fetch Outside Weather (Orchestration)
            coords = _geocode(location)
            if not coords:
                return {"status": "failed", "error": f"City not found: {location}"}, 404
            
            lat, lon = coords
            weather_res = requests.get(
                WEATHER_URL,
                params={"lat": lat, "lon": lon, "appid": API_KEY, "units": "metric"},
                timeout=10
            )
            weather_res.raise_for_status()
            owm_data = weather_res.json()

            # 4. Merge Data
            # We append the outside data directly to the dictionary from the IoT device
            row_to_insert = {
                **local_values,
                "outdoor_temp": owm_data["main"]["temp"],
                "outdoor_humidity": owm_data["main"]["humidity"],
                "outdoor_weather": owm_data["weather"][0]["description"],
                "location": location,
            }

            # 5. Safe BigQuery Insertion
            # table_id should be your 'project.dataset.table' string
            errors = client.insert_rows_json(WEATHER_TABLE_PATH, [row_to_insert])
            
            if errors:
                return {"status": "failed", "error": f"BigQuery Insert Error: {errors}"}, 500

            return {"status": "success", "inserted_data": row_to_insert}, 200

        except requests.exceptions.RequestException as e:
            return {"status": "failed", "error": f"External API Error: {str(e)}"}, 502
        except Exception as e:
            return {"status": "failed", "error": str(e)}, 500


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

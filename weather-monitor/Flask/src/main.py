import os

from flask import Flask, request

try:
    from src.clients import PASSWORD_HASH, WEATHER_TABLE_PATH, client, df
except ImportError:
    from clients import PASSWORD_HASH, WEATHER_TABLE_PATH, client, df

app = Flask(__name__)


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


# For exercise 3: /get_outdoor_weather — to be implemented.


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)

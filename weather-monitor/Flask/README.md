# Weather Monitor — Flask API

Small Flask service that receives indoor weather readings and writes them to BigQuery. Designed to run locally (Docker Compose) and to be deployed to Google Cloud Run.

## Project layout

```
Flask/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── send_request.sh          # example curl commands
└── src/
    ├── main.py              # Flask app + route handlers
    ├── clients.py           # env loading, BigQuery client, config
    └── secret_manager.py    # Secret Manager helpers (kept for future use)
```

## Endpoints

| Method | Path | Body | Description |
| --- | --- | --- | --- |
| `POST` | `/send-to-bigquery` | `{"passwd": "<hash>", "values": {"date": "...", "time": "...", "indoor_temp": 23, "indoor_humidity": 67}}` | Inserts a row into the weather table. |

The `passwd` field must match `PASSWORD_HASH` in [src/clients.py](src/clients.py).

## Local development

```bash
cd weather-monitor/Flask
docker compose up --build
```

Docker Compose loads the repo-root `.env` (see [docker-compose.yml](docker-compose.yml)) and exposes the API on `http://localhost:8080`.

Alternatively, without Docker:

```bash
pip install -r requirements.txt
python src/main.py
```

## Configuration — environment variables

| Variable | Required where | Purpose | Default |
| --- | --- | --- | --- |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Local only | Inline service-account JSON, loaded from `.env`. **Not set in Cloud Run** — ADC is used instead. | — |
| `GCP_PROJECT_ID` | Local + Cloud Run | GCP project for the BigQuery client. | `data-buckets-489022` |
| `DATABASE_NAME` | Optional | BigQuery dataset. | `weather_records` |
| `WEATHER_TABLE_NAME` | Optional | BigQuery table. | `weather-data` |
| `PORT` | Cloud Run (injected) | Port gunicorn binds to. | `8080` |

## Deploying to Cloud Run — what to configure on the container

1. **Service account attached to the revision**
   Attach `assignment-1@data-buckets-489022.iam.gserviceaccount.com` (or an equivalent). This is how the container authenticates to BigQuery without any JSON key baked in.

2. **IAM roles on that service account**
   - `roles/bigquery.jobUser` — run queries.
   - `roles/bigquery.dataEditor` on the `weather_records` dataset — insert rows.
   - `roles/bigquery.dataViewer` on the dataset containing the startup probe table — the app runs `SELECT * FROM <WEATHER_TABLE_PATH> LIMIT 10` at startup to learn column dtypes.

3. **Environment variables to set on the revision**
   - `GCP_PROJECT_ID` (required)
   - `DATABASE_NAME` / `WEATHER_TABLE_NAME` (optional — set only if they differ from the defaults)
   - **Do not** set `GOOGLE_SERVICE_ACCOUNT_JSON` — ADC handles auth via the attached service account.

4. **Port**
   Leave default. Cloud Run injects `PORT=8080`; the Dockerfile binds `${PORT:-8080}` so no override is needed.

5. **Build & deploy**
   ```bash
   gcloud builds submit --tag gcr.io/data-buckets-489022/weather-flask .
   gcloud run deploy weather-flask \
     --image gcr.io/data-buckets-489022/weather-flask \
     --service-account assignment-1@data-buckets-489022.iam.gserviceaccount.com \
     --set-env-vars GCP_PROJECT_ID=data-buckets-489022 \
     --region europe-west1 \
     --allow-unauthenticated
   ```

## Testing

See [send_request.sh](send_request.sh) for example curl commands. Replace `<YOUR_PSWD>` with the value of `PASSWORD_HASH` in [src/clients.py](src/clients.py).

## Security TODO

`PASSWORD_HASH` is currently hardcoded in [src/clients.py](src/clients.py) for development convenience. Before any real deployment, migrate it to Secret Manager using the helpers already in [src/secret_manager.py](src/secret_manager.py) and grant the service account `roles/secretmanager.secretAccessor`.

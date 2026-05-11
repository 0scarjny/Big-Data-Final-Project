# Weather Monitor — Streamlit Dashboard

The UI tier of the home weather monitor. Reads sensor history from
**Google BigQuery** and live outdoor conditions / forecasts from the
project's **Flask service**. Designed to run on **Google Cloud Run** but
also works locally and on Streamlit Community Cloud.

## What it shows

- **Overview** — live indoor temperature, humidity, eCO₂, outdoor temperature
  KPI cards with delta vs the previous reading; last-24h sparklines; current
  outdoor weather card; 5-day forecast strip. Auto-refreshes every minute.
- **Indoor History** — date-range filter, line charts per metric, daily
  min/avg/max summary table, CSV export.
- **Outdoor & Comparison** — indoor vs outdoor temperature/humidity overlays,
  hour-of-day × day-of-week heatmap, outdoor weather mix bar chart.
- **About** — schema and refresh behaviour reference.

## Project layout

```
web-dashboard/
├── streamlit_app.py          # entry point, st.navigation
├── config.py                 # secrets/env loader
├── data/
│   ├── bigquery_client.py    # cached BigQuery query helpers
│   ├── queries.py            # parameterised SQL
│   └── flask_api.py          # client for /get_outdoor_weather, /get_forecast
├── components/
│   ├── metrics.py            # KPI cards
│   ├── charts.py             # Altair builders
│   └── filters.py            # sidebar widgets
├── views/                    # one module per page
│   ├── overview.py
│   ├── indoor.py
│   ├── outdoor.py
│   └── about.py
├── .streamlit/
│   ├── config.toml           # theme + server settings
│   └── secrets.toml.example  # template — copy to secrets.toml locally
├── requirements.txt
├── Dockerfile
└── .dockerignore
```

## Authentication (login gate)

The dashboard is protected by [streamlit-authenticator](https://github.com/mkhorasani/Streamlit-Authenticator). Users are defined entirely in `.streamlit/secrets.toml` under `[auth]`:

```toml
[auth]
enabled            = true
cookie_name        = "weather_dashboard_auth"
cookie_key         = "<long random string>"   # generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`
cookie_expiry_days = 1
auto_hash          = true                      # see "Pre-hashing passwords" below

[auth.credentials.usernames.admin]
email      = "admin@example.com"
first_name = "Admin"
last_name  = "User"
password   = "change-me"
roles      = ["admin"]
```

Add more users by repeating the `[auth.credentials.usernames.<name>]` block. To disable the gate entirely (e.g. behind a VPN), set `auth.enabled = false`.

### Pre-hashing passwords

With `auto_hash = true`, the library hashes the plaintext password in memory on each cold start. That's convenient but leaves the cleartext password on disk. For production:

```bash
python hash_password.py
# Password to hash: ********
# $2b$12$… (paste this into secrets.toml as the password value)
```

Then set `auto_hash = false`. The library will treat each `password` field as an already-bcrypt-hashed value.

## Configuration — environment / secrets

Every project-specific value is resolved in this order: `st.secrets` →
environment variable → built-in default. Nothing sensitive is hard-coded.

| Key (env var)         | Secrets path                  | Purpose                                            | Default                                                            |
| --------------------- | ----------------------------- | -------------------------------------------------- | ------------------------------------------------------------------ |
| `GCP_PROJECT_ID`      | `app.gcp_project_id`          | GCP project containing the BigQuery table.         | `data-buckets-489022`                                              |
| `BQ_DATASET`          | `app.bq_dataset`              | BigQuery dataset.                                  | `weather_records`                                                  |
| `BQ_TABLE`            | `app.bq_table`                | BigQuery table.                                    | `weather-data`                                                     |
| `FLASK_BASE_URL`      | `app.flask_base_url`          | Base URL of the Flask service.                     | `https://flask-app-868833155300.europe-west6.run.app`              |
| `FLASK_SHARED_SECRET` | `app.flask_shared_secret`     | `PASSWORD_HASH` value the Flask service expects.   | _empty — outdoor/forecast disabled until set_                      |
| `DEFAULT_LOCATION`    | `app.default_location`        | Fallback city for forecast when no reading exists. | `Lausanne`                                                         |
| `REFRESH_INTERVAL_S`  | `app.refresh_interval_s`      | Auto-refresh interval (seconds) for the live KPIs. | `60`                                                               |
| _(no env var)_        | `gcp_service_account.*`       | Inline service-account JSON for local / Streamlit Cloud. **Omit on Cloud Run** — ADC is used instead. | _absent_ |

## Local development

```bash
cd weather-monitor/web-dashboard
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# Edit secrets.toml: paste your service-account JSON into [gcp_service_account]
# and the Flask shared secret into [app].flask_shared_secret

streamlit run streamlit_app.py
# → http://localhost:8501
```

`.streamlit/secrets.toml` is gitignored at the repo root — never commit it.

## Deploy to Google Cloud Run

The app is designed so that on Cloud Run **no service-account JSON is needed
on the container** — the attached service account provides credentials via
Application Default Credentials.

### 1. Service account & IAM

Reuse `assignment-1@data-buckets-489022.iam.gserviceaccount.com` (the one the
Flask service uses). It already has:

- `roles/bigquery.jobUser` — to submit query jobs.
- `roles/bigquery.dataViewer` on the `weather_records` dataset — to read the
  table.

The dashboard only reads, so no `dataEditor` is required.

### 2. Build & deploy

```bash
cd weather-monitor/web-dashboard

gcloud builds submit --tag gcr.io/data-buckets-489022/weather-dashboard

gcloud run deploy weather-dashboard \
  --image gcr.io/data-buckets-489022/weather-dashboard \
  --service-account assignment-1@data-buckets-489022.iam.gserviceaccount.com \
  --region europe-west6 \
  --allow-unauthenticated \
  --port 8080 \
  --set-env-vars "GCP_PROJECT_ID=data-buckets-489022,BQ_DATASET=weather_records,BQ_TABLE=weather-data,FLASK_BASE_URL=https://flask-app-868833155300.europe-west6.run.app,DEFAULT_LOCATION=Lausanne"
```

For `FLASK_SHARED_SECRET`, use Secret Manager rather than a plain env var:

```bash
echo -n "$YOUR_PASSWORD_HASH" | gcloud secrets create FLASK_SHARED_SECRET --data-file=-
gcloud run services update weather-dashboard \
  --region europe-west6 \
  --update-secrets FLASK_SHARED_SECRET=FLASK_SHARED_SECRET:latest
```

Grant the runtime SA `roles/secretmanager.secretAccessor`.

**Do not** set a `gcp_service_account` secret on Cloud Run — leave it absent
so the BigQuery client falls back to ADC.

### 3. Verify

```bash
gcloud run services describe weather-dashboard --region europe-west6 \
  --format='value(status.url)'
# Open the URL — Overview page should load with live KPIs.

gcloud run services logs read weather-dashboard --region europe-west6 --limit 50
# No auth or import errors expected.
```

## Deploy to Streamlit Community Cloud (alternative)

1. Push this repo to GitHub.
2. On [share.streamlit.io](https://share.streamlit.io), create a new app
   pointing at `weather-monitor/web-dashboard/streamlit_app.py`.
3. Open **Settings → Secrets** and paste the contents of
   `.streamlit/secrets.toml.example` filled in (service-account JSON + Flask
   secret).
4. Deploy.

## Troubleshooting

| Symptom                                                | Likely cause                                                                  |
| ------------------------------------------------------ | ----------------------------------------------------------------------------- |
| "BigQuery unavailable: …"                              | Service account missing IAM roles, or `GCP_PROJECT_ID` wrong.                 |
| "Live outdoor data unavailable" on Overview            | `FLASK_SHARED_SECRET` empty or Flask service unreachable.                     |
| Forecast block missing                                 | Same as above — the forecast endpoint shares the same secret.                 |
| Daily summary table empty for a date you know has data | Check `date` column format in BigQuery — must be `YYYY-MM-DD`.                |
| `db-dtypes` import errors                              | Reinstall: `pip install -r requirements.txt --upgrade`.                       |
| Heatmap empty                                          | Fewer than ~24 samples in the range — pick a wider window.                    |

## How it fits with the rest of the project

```
M5Stack device ── POST /send-to-bigquery ──▶  Flask  ──▶  BigQuery
                                                  ▲              ▲
                                                  │              │
       Streamlit dashboard ──── POST /get_outdoor_weather, /get_forecast
                            ──── SELECT (direct BigQuery client) ─┘
```

The dashboard is read-only and never writes back to BigQuery.

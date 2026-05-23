# RERA Rajasthan Intelligence MVP

MVP for loading Rajasthan RERA project listings plus raw project-detail JSON into PostgreSQL, tracking JSON changes over time, and exploring the data in Streamlit locally or on Streamlit Community Cloud.

## What it does

- Imports the CSV project list into `rera_projects`
- Stores the latest raw project JSON in `rera_projects.raw_json`
- Stores historical JSON versions in `rera_project_snapshots`
- Computes a SHA256 hash for every JSON document
- Tracks field-level raw JSON changes in `rera_project_changes`
- Stores manual comparable prices in `project_market_prices`
- Stores automated price candidates in `project_price_candidates`
- Stores saved ROI scenarios in `project_roi_cases`
- Fetches fresh raw project JSON directly from Rajasthan RERA detail endpoints without needing the heavy browser scraper for daily updates
- Supports local automatic afternoon sync with retry on the next scheduled check when the machine comes back online
- Keeps raw JSON as the source of truth
- Provides a local Streamlit dashboard for search, filters, detail view, map inspection, market pricing, ROI analysis, raw JSON inspection, and change logs

## Files

- `setup_db.py`: creates the PostgreSQL tables and indexes
- `ingest_existing_jsons.py`: imports the current CSV and local JSON files
- `daily_sync.py`: refreshes the CSV, fetches direct raw project JSON for missing or stale projects, and ingests updates
- `auto_update.py`: only runs the sync when it is due and internet is available
- `install_launch_agent.py`: installs a macOS LaunchAgent so the updater checks automatically in the background
- `dashboard.py`: Streamlit dashboard
- `streamlit_app.py`: Streamlit Community Cloud entrypoint
- `api.py`: FastAPI backend for health checks, AI ask endpoint, and WhatsApp webhook handling
- `rera_intel/`: shared config, schema, extraction, hashing, and ingest logic

## Prerequisites

- Python 3.10+
- PostgreSQL running locally or remotely
- A database already created, for example `rera_rajasthan`

## Setup

1. Create and activate a virtual environment if you want one.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy the env template and edit it:

```bash
cp .env.example .env
```

4. Set `DATABASE_URL` in `.env`, for example:

```env
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rera_rajasthan
```

5. Optional:

```env
RERA_API_KEY=MySuperSecretApiKey_123
DETAIL_SYNC_MAX_PROJECTS_PER_RUN=100
DETAIL_SYNC_REFRESH_DAYS=30
DETAIL_SYNC_CANDIDATE_SCAN_MULTIPLIER=3
DETAIL_SYNC_FAILURE_COOLDOWN_HOURS=24
AUTO_SYNC_AFTERNOON_HOUR=14
AUTO_SYNC_AFTERNOON_MINUTE=0
AUTO_SYNC_INTERVAL_MINUTES=30
SERPAPI_KEY=your_serpapi_key
SERPAPI_GL=in
SERPAPI_HL=en
SERPAPI_LOCATION=Rajasthan, India
OPENAI_API_KEY=your_openai_api_key
OPENAI_SUMMARY_MODEL=gpt-5.5
WHATSAPP_VERIFY_TOKEN=your_verify_token
WHATSAPP_ACCESS_TOKEN=your_cloud_api_access_token
WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id
WHATSAPP_ALLOWED_NUMBERS=+9198xxxxxx01,+9198xxxxxx02
WHATSAPP_GRAPH_API_VERSION=v23.0
WHATSAPP_REPLY_MAX_CHARS=1200
RERA_CSV_PATH=rajasthan_rera_projects.csv
RERA_JSON_DIR=rera_project_detail_jsons
```

## Run

```bash
python setup_db.py
python ingest_existing_jsons.py
streamlit run streamlit_app.py
```

Optional backend for AI and WhatsApp:

```bash
uvicorn api:app --reload
```

If you are updating an existing local setup, run `python setup_db.py` again after pulling the latest code so PostgreSQL creates any newly added tables and indexes.

## Daily sync

```bash
python daily_sync.py
```

`daily_sync.py` now does the full lightweight update flow:

- refreshes the master project CSV from the Rajasthan RERA list API using `RERA_API_KEY`
- updates `rera_projects` from the latest listing data
- fetches raw detail JSON directly from Rajasthan RERA for projects that are missing JSON or are stale
- saves the raw JSON files back into `rera_project_detail_jsons`
- ingests each fetched JSON into PostgreSQL and records snapshots and field-level changes

This replaces the need to run the heavy browser scraper for normal daily updates.

## Automatic local updates

Install the macOS LaunchAgent once:

```bash
python install_launch_agent.py
```

What it does:

- checks every `AUTO_SYNC_INTERVAL_MINUTES` minutes
- runs once immediately after install or login
- performs a real sync only when:
  - it is due for the afternoon window
  - internet is available
- if the machine is offline at the scheduled time, the next background check will catch up automatically

Manual force-run:

```bash
python auto_update.py --force
```

## Notes

- The importer loads all CSV rows, even if a matching detail JSON is not available yet.
- If a project already exists and its JSON hash changes, a new snapshot is inserted and raw JSON field-level changes are recorded.
- If the hash is unchanged, the importer only refreshes `last_scraped_at` and current searchable metadata.
- Some Rajasthan RERA detail IDs can occasionally return server-side `500` errors. The sync loop skips those failures, records them in warnings, and continues with the rest of the batch.
- Recent detail-fetch failures are cooled down locally so the same broken Rajasthan RERA IDs do not get retried on every single run.
- The dashboard now includes a `Documents` tab with clickable document links resolved against the official Rajasthan RERA document host.
- If `OPENAI_API_KEY` is set, you can select a document and ask OpenAI to summarize supported PDF, image, KML, and KMZ files directly from the dashboard.
- KML and KMZ location files now render directly inside the project overview with an embedded map and extracted coordinates.
- The dashboard includes a `Market Pricing` tab with:
  - manual comparable price entry
  - automated price candidates discovered from SerpAPI Google search results
  - selected market price resolution with manual override priority
  - saved market listing table
  - ROI calculator
  - saved ROI scenarios
  - future-ready fields for later deeper sync without enabling restricted-page scraping yet
- Automated price discovery uses SERP result titles, snippets, and source URLs only. It does not directly scrape restricted 99acres, Magicbricks, or Housing pages in this phase.
- In the project detail page, use:
  - `Refresh prices for this project` to refresh one project
  - `Run weekly price sync` to process a capped batch using SerpAPI
- The dashboard supports:
  - project search
  - district filter
  - promoter filter
  - project type filter
  - approved year filter
  - changed recently filter
  - project detail page
  - embedded project map from KML or KMZ files
  - coordinate viewer
  - market pricing section
  - ROI scenario saving
  - document link viewer
  - OpenAI document summary action
  - raw JSON viewer
  - change log viewer
- The FastAPI backend supports:
  - `GET /health`
  - `POST /ask`
  - `GET /whatsapp/webhook`
  - `POST /whatsapp/webhook`
- WhatsApp webhook behavior:
  - reads inbound text messages
  - restricts replies to `WHATSAPP_ALLOWED_NUMBERS`
  - sends the question to the AI agent in `Auto` mode
  - shortens long replies for WhatsApp and limits project lists to the top 5 preview rows
  - logs inbound and outbound WhatsApp events in `whatsapp_message_logs`

## Streamlit Community Cloud deployment

Community Cloud deploys from a GitHub repository, not directly from a local folder. The official docs say you must connect GitHub to Streamlit Community Cloud and deploy by selecting a repository, branch, and entrypoint file. You can use `streamlit_app.py` as the entrypoint. Sources:

- [Deploy your app on Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
- [Connect your GitHub account](https://docs.streamlit.io/deploy/streamlit-community-cloud/get-started/connect-your-github-account)
- [File organization for Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/file-organization)

### Before deploying

1. Push this project to a GitHub repository.
2. In Streamlit Community Cloud, connect your GitHub account.
3. Create a new app using:
   - Repository: your GitHub repo
   - Branch: `main`
   - Main file path: `streamlit_app.py`
4. In the app's advanced settings, paste your secrets.

### Secrets for Cloud

Create your local file from the template if you want to mirror Cloud secrets:

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Recommended Cloud secrets:

```toml
DATABASE_URL = "postgresql://user:password@host:5432/rera_rajasthan"
OPENAI_API_KEY = "sk-..."
RERA_API_KEY = "your_rera_api_key"
SERPAPI_KEY = "your_serpapi_key"
SERPAPI_GL = "in"
SERPAPI_HL = "en"
SERPAPI_LOCATION = "Rajasthan, India"
OPENAI_SUMMARY_MODEL = "gpt-5.5"
```

### Important deployment notes

- Do not commit `.env`, `openaikey`, `.streamlit/secrets.toml`, or local logs/data files.
- The dashboard can run in the cloud using your remote PostgreSQL database. It does not need the full local `rera_project_detail_jsons/` folder if the database already contains the raw JSON.
- Test locally from the repository root with:

```bash
streamlit run streamlit_app.py
```

## Daily refresh on GitHub Actions

The repository now includes a scheduled workflow at `.github/workflows/daily-sync.yml`.

What it does:

- runs every day at `14:00 IST` (`08:30 UTC`)
- can also be triggered manually from the GitHub Actions tab with `Run workflow`
- ensures the PostgreSQL schema exists with `python setup_db.py`
- runs `python daily_sync.py` against your remote database
- writes temporary CSV/JSON/state files only on the GitHub runner, not to your laptop

### Required GitHub repository secrets

Add these in your GitHub repo under `Settings -> Secrets and variables -> Actions -> Secrets`:

```text
DATABASE_URL
RERA_API_KEY
OPENAI_API_KEY
SERPAPI_KEY
```

Only `DATABASE_URL` and `RERA_API_KEY` are strictly required for the daily RERA sync job itself. `OPENAI_API_KEY` and `SERPAPI_KEY` are useful for the dashboard's other features.

### Optional GitHub repository variables

You can also add these under `Settings -> Secrets and variables -> Actions -> Variables` if you want to tune the sync without changing code:

```text
DETAIL_SYNC_MAX_PROJECTS_PER_RUN
DETAIL_SYNC_REFRESH_DAYS
DETAIL_SYNC_CANDIDATE_SCAN_MULTIPLIER
DETAIL_SYNC_FAILURE_COOLDOWN_HOURS
SERPAPI_GL
SERPAPI_HL
SERPAPI_LOCATION
OPENAI_SUMMARY_MODEL
GEOCODING_ENDPOINT
GEOCODING_REVERSE_ENDPOINT
GEOCODING_USER_AGENT
GEOCODING_EMAIL
```

### Important note

This daily workflow will populate and refresh the remote database directly. It is enough to keep `rera_projects` and detail JSON data moving forward without your computer being on.

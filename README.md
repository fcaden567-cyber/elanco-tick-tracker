# UK Tick Sightings – Backend

**Elanco Placement Programme 2026 – Technical Task (Backend)**
Author: Caden Fernandes

A backend for a UK tick-sightings tracker, in the spirit of Elanco's US Parvovirus Tracker. It
consumes crowdsourced sightings from the Elanco tick API, cleans and de-duplicates them into a
local database, and serves search, reporting and statistical-insight endpoints for a web front end.

```
 Elanco API ──► resilient client ──► validate / repair / de-dupe ──► SQLite ──► REST API (FastAPI)
 (upstream)     retries, backoff,     quarantine bad records,        indexed     search · reports ·
                circuit breaker,      audit every run                 storage     insights · pipeline
                per-city fallback
```

## Features at a glance

| Requirement | What's built | Where |
|---|---|---|
| **Data handling** | Scheduled + on-demand sync, validation, repair, de-duplication, quarantine of bad records, idempotent upserts, change detection | `app/ingestion.py`, `app/upstream.py` |
| **Search & filtering** | Time range (year / month / day / datetime precision), city, region, species, radius around a lat/lon, free text, pagination, sorting | `GET /api/v1/sightings` |
| **Data reporting** | Sightings per city or UK region, per species, trends by day / week / month / year (optionally split by group), seasonality, summary | `/api/v1/reports/*` |
| **AI/ML insights (extension)** | Back-tested forecast with model selection, trend and seasonality significance tests, hotspot early-warning, anomaly detection, all with multiple-testing correction | `/api/v1/insights/*` |
| **Error handling** | Retries with backoff + jitter, `Retry-After`, circuit breaker, fallback fetch strategy, serving stale data with headers, consistent error envelope with request IDs | `app/upstream.py`, `app/errors.py` |

## Running it

Requires **Python 3.11+** (developed on 3.13). No database server is needed: SQLite ships with Python.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:create_app --factory --reload
```

Then open **http://127.0.0.1:8000/docs** for interactive Swagger docs. The app syncs from the
upstream API on startup and then every 60 minutes.

Other commands:

```bash
python -m app.cli sync     # one-off sync from the command line
pytest                     # run the test suite (39 tests, no network needed)
```

Or with Docker:

```bash
docker build -t tick-tracker .
docker run -p 8000:8000 tick-tracker
```

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `UPSTREAM_BASE_URL` | `https://dev-task.elancoapps.com/data` | Source API |
| `DATABASE_PATH` | `data/ticks.db` | SQLite file |
| `SYNC_ON_STARTUP` | `true` | Sync when the app starts |
| `SYNC_INTERVAL_MINUTES` | `60` | Background refresh interval (`0` = off) |
| `STALE_AFTER_MINUTES` | `180` | When responses get `X-Data-Stale: true` |
| `UPSTREAM_MAX_RETRIES` | `4` | Retries per upstream request |
| `UPSTREAM_TIMEOUT_SECONDS` | `20` | Per-request timeout |
| `BREAKER_FAILURE_THRESHOLD` / `BREAKER_RESET_SECONDS` | `3` / `300` | Circuit breaker |
| `ADMIN_TOKEN` | *(unset)* | If set, `POST /api/v1/ingestion/run` needs `X-Admin-Token` |

## API overview

| Method & path | Description |
|---|---|
| `GET /health` | Liveness, stored count, last sync, upstream circuit state |
| `GET /api/v1/sightings` | Search & filter (see parameters below) |
| `GET /api/v1/sightings/{id}` | One sighting |
| `GET /api/v1/locations` · `/regions` · `/species` | Reference lists with counts (and coordinates for mapping) |
| `GET /api/v1/reports/summary` | Headline numbers |
| `GET /api/v1/reports/sightings-per-region?group_by=city\|region` | Sightings per region |
| `GET /api/v1/reports/species` | Sightings per species |
| `GET /api/v1/reports/trends?interval=week\|month&group_by=species` | Trends over time, zero-filled |
| `GET /api/v1/reports/seasonality` | Counts by calendar month |
| `GET /api/v1/insights/overview` | Is activity rising? Is there a tick season? (with p-values) |
| `GET /api/v1/insights/forecast?months=6` | Monthly forecast with 95% interval |
| `GET /api/v1/insights/hotspots` | Cities with unusually high recent activity |
| `GET /api/v1/insights/anomalies` | Months with unusual spikes |
| `POST /api/v1/ingestion/run` | Trigger a sync now |
| `GET /api/v1/ingestion/runs` | Sync history |
| `GET /api/v1/data-quality` | What the last sync rejected / repaired / de-duplicated and why |

**Every report and insight endpoint accepts the same filters as search**, so the dashboard
can, for example, show trends for one species in Scotland only.

| Filter | Example | Notes |
|---|---|---|
| `start`, `end` | `start=2023-06&end=2023-08` | Inclusive. `YYYY`, `YYYY-MM`, `YYYY-MM-DD` or ISO datetime. `end=2023-08` covers all of August |
| `location` | `location=London&location=Leeds` | Case-insensitive, repeatable |
| `region` | `region=Scotland` | UK region derived from the city |
| `species` | `species=marsh tick` | Case-insensitive, repeatable |
| `near_lat`, `near_lon`, `radius_km` | `near_lat=53.48&near_lon=-2.24&radius_km=60` | "Sightings near me" |
| `q` | `q=ixodes` | Free text over city, region, species and latin name |
| `page`, `page_size`, `sort` | `page=2&page_size=50&sort=-date` | `page_size` capped at 500 |

### Example requests

```bash
# London sightings in summer 2023
curl "localhost:8000/api/v1/sightings?location=London&start=2023-06&end=2023-08"

# Sightings per UK region in 2024
curl "localhost:8000/api/v1/reports/sightings-per-region?group_by=region&start=2024&end=2024"

# Weekly trend for one species
curl "localhost:8000/api/v1/reports/trends?interval=week&species=Marsh%20tick&start=2024-01"

# 6-month forecast for Scotland
curl "localhost:8000/api/v1/insights/forecast?months=6&region=Scotland"
```

Errors always use the same shape:

```json
{"error": {"code": "invalid_range", "message": "'start' must be before 'end'",
           "details": {"start": "2024", "end": "2023"}, "request_id": "a7c48dbae412"}}
```

## Key design choices

- **Ingest into our own store rather than proxying the upstream API.** The upstream API is
  rate-limited, can only filter by exact, case-sensitive city or species, and has no date
  filtering or aggregation. Copying the data locally makes queries fast, gives full filtering and
  reporting, and means **an upstream outage doesn't take our API down**: we keep serving the last
  good data and say so in the `X-Data-Stale` / `X-Data-Last-Synced` headers.
- **Python + FastAPI.** Type-checked request validation, automatic OpenAPI docs and a small,
  readable codebase.
- **SQLite.** No infrastructure to install, but still real SQL with indexes, transactions and
  aggregation. All reporting runs as SQL `GROUP BY`, so it scales with the database rather than
  with Python memory. The SQL is portable to PostgreSQL.
- **Never silently drop data.** Invalid and duplicate records go to a `rejected_records` table with
  a reason, and every sync is logged in `ingestion_runs`, so you can see exactly what happened.
- **Honest statistics.** The insight endpoints report p-values and back-test results and apply a
  false-discovery-rate correction. On the current dataset they correctly report *no significant
  trend or seasonality*, rather than reading patterns into noise.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the full write-up: architecture, data
handling strategy, error handling, insight methodology and what I'd do with more time.

## Project layout

```
app/
  main.py         app factory, middleware, background sync loop, /health
  config.py       settings from environment variables
  upstream.py     resilient HTTP client: retries, backoff, circuit breaker, fallback
  ingestion.py    validate → repair → de-duplicate → persist pipeline + SyncService
  db.py           SQLite schema
  repository.py   search and aggregation queries
  insights.py     forecasting & statistical tests (pure Python)
  reference.py    city → region/coordinates, species → latin name
  errors.py       error envelope + exception handlers
  cli.py          `python -m app.cli sync`
  api/            routers: sightings, reports, insights, admin (pipeline)
tests/            39 tests against a mocked, deliberately messy upstream
docs/             architecture & thought process
```

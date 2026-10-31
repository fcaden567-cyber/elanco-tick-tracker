# Architecture & thought process

## 1. Understanding the source

Before designing anything I profiled the upstream API (`https://dev-task.elancoapps.com/data`):

- Endpoints: `/tick-sightings` (everything), `/tick-sightings/{id}`, `/tick-sightings/city/{city}`,
  `/tick-sightings/species/{species}`, `/stats`, `/health`. It is read-only and has no auth.
- It **rate-limits** (HTTP 429) and can return 500s.
- City and species filters are **exact and case-sensitive**. There is **no date filtering, no
  pagination and no aggregation**.
- Data: 1,000 records, 2012–2024, 14 UK cities, 5 species, each with
  `id, date, location, species, latinName`.

The current feed is clean, but the brief asks for duplicates and incomplete data to be handled,
and a crowdsourced feed will get messy over time. So the pipeline assumes nothing and the tests
feed it deliberately broken data.

## 2. Architecture

```
                 ┌───────────────────────── FastAPI app ─────────────────────────┐
                 │                                                               │
 Elanco API ◄────┤ TickApiClient (upstream.py)                                   │
  (rate-limited) │   timeout · throttle · retry+backoff+jitter · Retry-After    │
                 │   circuit breaker · per-city fallback                         │
                 │        │ raw JSON                                             │
                 │        ▼                                                      │
                 │ Ingestion pipeline (ingestion.py)                             │
                 │   validate → repair/normalise → de-duplicate → upsert        │
                 │   (one transaction per run; chunked writes)                   │
                 │        │                    │                                 │
                 │        ▼                    ▼                                 │
                 │  sightings table     rejected_records + ingestion_runs        │
                 │        │   (SQLite, WAL, indexed)                             │
                 │        ▼                                                      │
                 │ repository.py (SQL GROUP BY) ─► reports / search routers      │
                 │ insights.py (statistics)     ─► insights router               │
                 └───────────────────────────────────────────────────────────────┘
         background loop: sync on startup, then every N minutes (+ POST /ingestion/run)
```

**Why ingest instead of proxy?** Proxying would inherit every limitation above: rate limits,
case-sensitive filters, no date ranges, no aggregation, and downtime whenever upstream is down.
An ingest-and-serve design decouples our availability from theirs. Reads are local and fast,
and if upstream fails we still serve the last good data, flagged as stale.

**Why SQLite?** It needs no infrastructure for a reviewer to run, but is still a real relational
store with indexes and transactions. WAL mode lets the API read while a sync writes. Moving to
PostgreSQL would mean changing `db.py` and the few SQLite-specific date functions in
`repository.py`.

**Why pure-Python statistics?** The dataset is small, and the methods (least squares, Poisson and
chi-square tests, Benjamini–Hochberg) are short and fully explainable. Avoiding numpy, pandas and
sklearn keeps the install tiny and makes every number traceable.

## 3. Data handling

| Problem | Handling |
|---|---|
| Missing `id`, `date`, `location`, or both `species` and `latinName` | Rejected and quarantined with a reason |
| Unparseable dates | Several formats accepted (ISO with `T` or space, fractions, `Z` and offsets → UTC, `DD/MM/YYYY`); otherwise rejected |
| Implausible dates | Future dates (beyond 1 day of clock skew) and dates before 1990 rejected |
| Messy text | Whitespace trimmed and collapsed. City and species matched case-insensitively to canonical names. City aliases folded (`Newcastle upon Tyne` → `Newcastle`) |
| Missing `latinName` | Imputed from the species reference table |
| Missing `species` but known `latinName` | Species recovered from the latin name |
| Unknown city | **Kept** (title-cased) with region `Unknown` and listed in `/data-quality`, so new locations aren't lost |
| Same `id` twice in one feed | Second copy quarantined as a duplicate |
| Same sighting under a different `id` (same timestamp, city, species) | Treated as a double submission and quarantined, both within a feed and against already-stored data |
| Record changed upstream | Detected by a content hash. Counted as `updated` and overwritten |
| Re-running a sync | Idempotent: unchanged records count as `unchanged`, nothing is duplicated |

Every run is recorded in `ingestion_runs` (fetched / inserted / updated / unchanged / repaired /
duplicates / rejected / warnings / error). `GET /api/v1/data-quality` exposes the breakdown and
examples, so data issues are visible rather than silently dropped.

### Large datasets

- **Chunked writes** (500 rows per `executemany`). Existing-record lookups are done per chunk
  with `IN (...)`, so memory stays bounded and we never load the whole table.
- **One transaction per run.** A crash part-way through rolls back cleanly and the previous data
  stays intact. Runs left as `running` by a killed process are marked failed on the next start.
- **Indexes** on `observed_at`, `(location, observed_at)`, `(species, observed_at)` and `region`
  match the filter patterns.
- **Aggregation in SQL**, not Python. Search is paginated (`page_size` ≤ 500).
- **Per-city fallback** also works as a partitioning strategy: if the bulk endpoint becomes too
  large or unreliable, the dataset can be rebuilt in smaller per-city pieces.

## 4. Error handling

**Upstream failures**, from the inside out:
1. A timeout on every request, plus a minimum gap between calls so we don't trigger the rate
   limit ourselves.
2. Retries for transient failures only (timeouts, connection errors, 408/425/429/5xx, invalid
   JSON), using exponential backoff with jitter and honouring `Retry-After`. Other 4xx responses
   fail immediately, because retrying won't fix them.
3. A circuit breaker: after N consecutive failed calls it stops calling upstream for a cool-down
   period, then lets one trial call through ("half-open").
4. A fallback strategy: if `/tick-sightings` fails, rebuild from `/tick-sightings/city/{city}` for
   every known city. If some cities fail, the run is marked `partial` with warnings.
5. Graceful degradation: if everything fails, the run is `failed` with the reason, and the API
   keeps serving stored data. `X-Data-Stale: true` and `/health` make the state visible.

**Our own API**: every error, including validation errors, 404s and unexpected exceptions,
returns one envelope `{"error": {code, message, details, request_id}}`. Unexpected exceptions are
logged with the request ID and never leak stack traces. Invalid input gets a precise message,
e.g. `start` after `end`, a bad date format, or a radius search missing one of its three
parameters.

## 5. Insights (AI/ML extension)

Rather than producing numbers that look impressive but mean nothing, each insight says how far
it can be trusted:

- **Trend** (`/insights/overview`): least-squares slope of yearly counts with a significance test.
  An incomplete final year is dropped so it can't fake a decline.
- **Seasonality** (`/insights/overview`): chi-square goodness-of-fit against an even spread across
  months. It returns peak months and whether the pattern is significant.
- **Forecast** (`/insights/forecast`): three candidate models (historical mean, seasonal average,
  linear trend × seasonal index) are **back-tested on the last 12 months held out**. The model with
  the lowest MAE is refitted on all data and forecasts with a 95% interval. On seasonal data it
  picks a seasonal model; on the current, flat data it correctly picks the mean.
- **Hotspots** (`/insights/hotspots`): each city's last *N* months against its own historical rate,
  using a Poisson test. This is outbreak-style early warning, like the Parvovirus Tracker.
- **Anomalies** (`/insights/anomalies`): months with significantly more sightings than the
  **median** for that calendar month. The median is used so a spike can't inflate its own
  baseline and hide itself.
- **Multiple testing**: hotspots test 14 cities and anomalies test 156 months, so some will look
  significant by chance. Both apply a **Benjamini–Hochberg** false-discovery-rate correction. In
  the live data, Southampton's 2024 count is 2.5× its baseline (raw p = 0.007), but after
  correction it is a `watch`, not a `high` alert.

**What the data says today:** no significant long-term trend (p ≈ 0.99) and no significant
seasonality (p ≈ 0.49). The data looks close to uniformly random, and the system says so
instead of reading patterns into noise. The tests check that the same code does detect real
seasonality, growth, surges and spikes when they exist.

## 6. Testing

39 pytest tests, with no network access needed (the upstream is mocked with `httpx.MockTransport`):
- Pipeline: validation, repair, de-duplication, date parsing, idempotency, change detection.
- Resilience: 429 + `Retry-After`, 5xx retry limits, no retry on 4xx, timeouts, bad JSON,
  circuit breaker states, per-city fallback, serving stale data during an outage.
- API: every filter, pagination, sorting, error envelopes, reports (including weekly zero-fill),
  insights.
- Statistics: helper functions, plus "does it detect a real signal / ignore noise" cases.

## 7. What I'd do with more time

- **PostgreSQL + PostGIS** for real geospatial queries, and a migration tool (Alembic).
- **Incremental sync.** Upstream has no "changed since" parameter, so every sync re-reads
  everything. With an upstream cursor or ETag we could fetch only the changes. I'd also stream-parse
  very large responses (e.g. `ijson`) instead of loading them into memory.
- **Deletions/tombstones.** Records that vanish upstream are currently kept. I'd track `last_seen_at`
  across runs and soft-delete records missing from several consecutive *complete* syncs.
- **Run ingestion as a separate worker** (e.g. a queue or scheduled job), so several API replicas
  don't each sync. Today the in-process loop plus a lock is fine for one instance.
- **Caching** of report responses (keyed by filters and last sync time), and keyset pagination
  for deep pages.
- **Better models** as data grows: per-city hierarchical/Bayesian models, weather and habitat
  covariates (temperature, humidity, woodland cover), and population normalisation, so "more
  sightings in London" isn't just "more people in London".
- **Ops**: structured JSON logs, Prometheus metrics (sync duration, rejection rate, upstream error
  rate), alerting on failed syncs, rate limiting and auth on our own API, and CI running the tests.
- **A front end**: a map and dashboard over these endpoints. Responses already include coordinates
  and zero-filled time series, ready for charting.

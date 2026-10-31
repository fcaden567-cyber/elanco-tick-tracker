"""SQLite storage: schema and connection helper.

SQLite keeps the project zero-infrastructure (no DB server to install) while
still giving us indexes, transactions and SQL aggregation. The SQL used here is
standard enough to move to PostgreSQL if the dataset outgrows a single file.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id            TEXT PRIMARY KEY,
    observed_at   TEXT NOT NULL,          -- ISO 8601, 'YYYY-MM-DDTHH:MM:SS'
    location      TEXT NOT NULL,          -- canonical city name
    region        TEXT NOT NULL,          -- derived from reference data
    species       TEXT NOT NULL,
    latin_name    TEXT,
    content_hash  TEXT NOT NULL,          -- detects records that changed upstream
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sightings_observed_at ON sightings (observed_at);
CREATE INDEX IF NOT EXISTS ix_sightings_location_time ON sightings (location, observed_at);
CREATE INDEX IF NOT EXISTS ix_sightings_species_time ON sightings (species, observed_at);
CREATE INDEX IF NOT EXISTS ix_sightings_region ON sightings (region);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger      TEXT NOT NULL,
    status       TEXT NOT NULL,           -- running | success | partial | failed
    source       TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    fetched      INTEGER DEFAULT 0,
    inserted     INTEGER DEFAULT 0,
    updated      INTEGER DEFAULT 0,
    unchanged    INTEGER DEFAULT 0,
    repaired     INTEGER DEFAULT 0,
    duplicates   INTEGER DEFAULT 0,
    rejected     INTEGER DEFAULT 0,
    warnings     TEXT,
    error        TEXT
);

-- Quarantine: every record we refused, with the reason, so data issues are
-- auditable instead of silently disappearing.
CREATE TABLE IF NOT EXISTS rejected_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES ingestion_runs (id),
    source_id    TEXT,
    kind         TEXT NOT NULL,           -- invalid | duplicate
    reason       TEXT NOT NULL,
    payload      TEXT,
    rejected_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rejected_run ON rejected_records (run_id);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        # WAL lets API reads carry on while an ingestion run is writing.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # A crash mid-run would leave a 'running' row behind forever.
        conn.execute(
            "UPDATE ingestion_runs SET status = 'failed', error = 'interrupted (process stopped)' "
            "WHERE status = 'running'"
        )
        conn.commit()
    finally:
        conn.close()

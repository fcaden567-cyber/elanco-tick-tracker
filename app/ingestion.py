"""Data pipeline: fetch -> validate & normalise -> de-duplicate -> persist.

Design rules:
  * Never trust the feed. Every record is validated; bad ones go to a
    quarantine table with a reason rather than being silently dropped.
  * Repair what can be repaired safely (whitespace, casing, a missing latin
    name that we can derive from the species) and count those repairs.
  * Idempotent: re-running a sync over the same feed changes nothing.
  * Atomic: a run's writes land in one transaction, so a crash half-way
    through never leaves the database half-updated.
  * Chunked: records are written in batches so memory and transaction size
    stay bounded as the dataset grows.
"""

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app import reference
from app.db import connect
from app.upstream import TickApiClient, UpstreamError

log = logging.getLogger(__name__)

EARLIEST_PLAUSIBLE_YEAR = 1990
CHUNK_SIZE = 500


class InvalidRecord(ValueError):
    pass


class SyncInProgressError(RuntimeError):
    pass


@dataclass(frozen=True)
class Sighting:
    id: str
    observed_at: str
    location: str
    region: str
    species: str
    latin_name: str | None

    @property
    def content_hash(self) -> str:
        key = "|".join([self.observed_at, self.location, self.species, self.latin_name or ""])
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @property
    def natural_key(self) -> tuple[str, str, str]:
        return self.observed_at, self.location, self.species


@dataclass
class Rejection:
    source_id: str | None
    kind: str  # "invalid" | "duplicate"
    reason: str
    payload: Any


@dataclass
class CleanResult:
    sightings: list[Sighting] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    repaired: int = 0

    @property
    def invalid(self) -> int:
        return sum(1 for r in self.rejections if r.kind == "invalid")

    @property
    def duplicates(self) -> int:
        return sum(1 for r in self.rejections if r.kind == "duplicate")


# -- validation & normalisation ---------------------------------------------


def _text(value: Any) -> str | None:
    """Trim and collapse whitespace; treat empty/non-scalar values as missing."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    cleaned = " ".join(str(value).split())
    return cleaned or None


def parse_timestamp(value: Any) -> datetime:
    text = _text(value)
    if not text:
        raise InvalidRecord("missing date")
    try:
        # Handles 'YYYY-MM-DD', 'T' or space separators, fractions, 'Z' and offsets.
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise InvalidRecord(f"unparseable date {text!r}") from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def normalise_record(raw: Any, now: datetime) -> tuple[Sighting, bool]:
    """Return a clean Sighting and whether any field had to be repaired."""
    if not isinstance(raw, dict):
        raise InvalidRecord("record is not a JSON object")

    sighting_id = _text(raw.get("id"))
    if not sighting_id:
        raise InvalidRecord("missing id")

    observed = parse_timestamp(raw.get("date"))
    if observed > now + timedelta(days=1):
        raise InvalidRecord("date is in the future")
    if observed.year < EARLIEST_PLAUSIBLE_YEAR:
        raise InvalidRecord(f"date before {EARLIEST_PLAUSIBLE_YEAR} is implausible")

    raw_location = _text(raw.get("location"))
    if not raw_location:
        raise InvalidRecord("missing location")
    location = reference.canonical_city(raw_location)

    raw_species = _text(raw.get("species"))
    raw_latin = _text(raw.get("latinName"))
    if raw_species:
        species = reference.canonical_species(raw_species)
    elif raw_latin and reference.species_for_latin(raw_latin):
        species = reference.species_for_latin(raw_latin)  # recover species from its latin name
    else:
        raise InvalidRecord("missing species")
    latin = reference.latin_for(species) or raw_latin

    repaired = (location, species, latin) != (raw.get("location"), raw.get("species"), raw.get("latinName"))
    sighting = Sighting(
        id=sighting_id,
        observed_at=observed.isoformat(timespec="seconds"),
        location=location,
        region=reference.region_for(location),
        species=species,
        latin_name=latin,
    )
    return sighting, repaired


def _source_id(raw: Any) -> str | None:
    return _text(raw.get("id")) if isinstance(raw, dict) else None


def clean_records(raw_records: Iterable[Any], now: datetime | None = None) -> CleanResult:
    now = now or datetime.now()
    result = CleanResult()
    seen_ids: set[str] = set()
    seen_keys: dict[tuple[str, str, str], str] = {}

    for raw in raw_records:
        try:
            sighting, repaired = normalise_record(raw, now)
        except InvalidRecord as exc:
            result.rejections.append(Rejection(_source_id(raw), "invalid", str(exc), raw))
            continue

        if sighting.id in seen_ids:
            result.rejections.append(Rejection(sighting.id, "duplicate", "id appears more than once in feed", raw))
            continue
        # Same moment, same city, same species under a different id is almost
        # certainly a double submission of one sighting.
        original = seen_keys.get(sighting.natural_key)
        if original:
            result.rejections.append(Rejection(sighting.id, "duplicate", f"same sighting as {original}", raw))
            continue

        seen_ids.add(sighting.id)
        seen_keys[sighting.natural_key] = sighting.id
        result.sightings.append(sighting)
        result.repaired += repaired
    return result


# -- persistence -------------------------------------------------------------

_UPSERT = """
INSERT INTO sightings (id, observed_at, location, region, species, latin_name, content_hash, first_seen_at, last_seen_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (id) DO UPDATE SET
    observed_at = excluded.observed_at,
    location = excluded.location,
    region = excluded.region,
    species = excluded.species,
    latin_name = excluded.latin_name,
    content_hash = excluded.content_hash,
    last_seen_at = excluded.last_seen_at
"""


def _chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def persist(conn: sqlite3.Connection, run_id: int, result: CleanResult, chunk_size: int = CHUNK_SIZE) -> dict:
    """Upsert clean sightings and quarantine rejections. Caller owns the transaction."""
    now = _utcnow()
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    rejections = list(result.rejections)

    for chunk in _chunks(result.sightings, chunk_size):
        marks = ",".join("?" * len(chunk))
        existing = {
            row["id"]: row["content_hash"]
            for row in conn.execute(f"SELECT id, content_hash FROM sightings WHERE id IN ({marks})",
                                    [s.id for s in chunk])
        }
        # Catch duplicates of sightings stored by an earlier run under another id.
        owners = {
            (row["observed_at"], row["location"], row["species"]): row["id"]
            for row in conn.execute(
                f"SELECT id, observed_at, location, species FROM sightings WHERE observed_at IN ({marks})",
                [s.observed_at for s in chunk],
            )
        }
        rows = []
        for s in chunk:
            owner = owners.get(s.natural_key)
            if s.id not in existing and owner and owner != s.id:
                rejections.append(Rejection(s.id, "duplicate", f"same sighting as stored {owner}", s.__dict__))
                continue
            if s.id not in existing:
                counts["inserted"] += 1
            elif existing[s.id] != s.content_hash:
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1
            rows.append((s.id, s.observed_at, s.location, s.region, s.species, s.latin_name, s.content_hash, now, now))
        conn.executemany(_UPSERT, rows)

    conn.executemany(
        "INSERT INTO rejected_records (run_id, source_id, kind, reason, payload, rejected_at) VALUES (?, ?, ?, ?, ?, ?)",
        [(run_id, r.source_id, r.kind, r.reason, json.dumps(r.payload, default=str)[:2000], now) for r in rejections],
    )
    counts["duplicates"] = sum(1 for r in rejections if r.kind == "duplicate")
    counts["rejected"] = sum(1 for r in rejections if r.kind == "invalid")
    return counts


# -- orchestration -----------------------------------------------------------


class SyncService:
    """Runs one ingestion at a time and records the outcome of each run."""

    def __init__(self, db_path: str, client: TickApiClient):
        self.db_path = db_path
        self.client = client
        self._lock = threading.Lock()
        self.last_success_at: datetime | None = self._load_last_success()

    def _load_last_success(self) -> datetime | None:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT MAX(finished_at) AS t FROM ingestion_runs WHERE status IN ('success', 'partial')"
            ).fetchone()
            return datetime.fromisoformat(row["t"]) if row and row["t"] else None
        finally:
            conn.close()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def run(self, trigger: str = "manual") -> dict:
        if not self._lock.acquire(blocking=False):
            raise SyncInProgressError("A sync is already running")
        try:
            return self._run(trigger)
        finally:
            self._lock.release()

    def _run(self, trigger: str) -> dict:
        conn = connect(self.db_path)
        try:
            with conn:
                run_id = conn.execute(
                    "INSERT INTO ingestion_runs (trigger, status, started_at) VALUES (?, 'running', ?)",
                    (trigger, _utcnow()),
                ).lastrowid
            known_cities = [r[0] for r in conn.execute("SELECT DISTINCT location FROM sightings")]
            try:
                fetched = self.client.fetch_all_sightings(fallback_cities=set(known_cities) | set(reference.CITIES))
            except UpstreamError as exc:
                log.error("Sync %s failed: %s", run_id, exc)
                with conn:
                    conn.execute(
                        "UPDATE ingestion_runs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                        (_utcnow(), str(exc), run_id),
                    )
                return self._summary(conn, run_id)

            cleaned = clean_records(fetched.records)
            status = "partial" if fetched.partial else "success"
            with conn:  # one transaction for all of this run's writes
                counts = persist(conn, run_id, cleaned)
                conn.execute(
                    """UPDATE ingestion_runs SET status = ?, source = ?, finished_at = ?, fetched = ?, inserted = ?,
                       updated = ?, unchanged = ?, repaired = ?, duplicates = ?, rejected = ?, warnings = ?
                       WHERE id = ?""",
                    (status, fetched.source, _utcnow(), len(fetched.records), counts["inserted"], counts["updated"],
                     counts["unchanged"], cleaned.repaired, counts["duplicates"], counts["rejected"],
                     json.dumps(fetched.warnings) if fetched.warnings else None, run_id),
                )
            summary = self._summary(conn, run_id)
            self.last_success_at = datetime.fromisoformat(summary["finished_at"])
            log.info("Sync %s %s: %s", run_id, status, {k: summary[k] for k in ("fetched", "inserted", "updated",
                                                                               "duplicates", "rejected")})
            return summary
        finally:
            conn.close()

    @staticmethod
    def _summary(conn: sqlite3.Connection, run_id: int) -> dict:
        row = dict(conn.execute("SELECT * FROM ingestion_runs WHERE id = ?", (run_id,)).fetchone())
        row["warnings"] = json.loads(row["warnings"]) if row["warnings"] else []
        return row

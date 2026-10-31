"""Read-side queries. Aggregation is pushed down into SQL so it scales with the
database rather than with Python memory."""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app import reference

TREND_PERIOD_SQL = {
    "day": "date(observed_at)",
    # SQLite: move forward to Sunday, then back 6 days = the ISO week's Monday.
    "week": "date(observed_at, 'weekday 0', '-6 days')",
    "month": "strftime('%Y-%m-01', observed_at)",
    "year": "strftime('%Y-01-01', observed_at)",
}

GROUP_COLUMNS = {"city": "location", "region": "region", "species": "species"}

SORTS = {"date": "observed_at ASC, id ASC", "-date": "observed_at DESC, id DESC",
         "location": "location ASC, observed_at DESC", "species": "species ASC, observed_at DESC"}


@dataclass
class SightingFilter:
    start: datetime | None = None          # inclusive
    end: datetime | None = None            # exclusive
    locations: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    species: list[str] = field(default_factory=list)
    near_cities: list[str] | None = None   # resolved from a lat/lon radius search
    text: str | None = None

    def where(self) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        if self.start:
            clauses.append("observed_at >= ?")
            params.append(self.start.isoformat(timespec="seconds"))
        if self.end:
            clauses.append("observed_at < ?")
            params.append(self.end.isoformat(timespec="seconds"))
        for column, values in (("location", self.locations), ("region", self.regions), ("species", self.species)):
            if values:
                clauses.append(f"lower({column}) IN ({','.join('?' * len(values))})")
                params.extend(v.strip().lower() for v in values)
        if self.near_cities is not None:
            if not self.near_cities:
                clauses.append("0")  # radius contains no known city
            else:
                clauses.append(f"location IN ({','.join('?' * len(self.near_cities))})")
                params.extend(self.near_cities)
        if self.text:
            like = "%" + self.text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append(
                "(location LIKE ? ESCAPE '\\' OR region LIKE ? ESCAPE '\\' OR species LIKE ? ESCAPE '\\' "
                "OR latin_name LIKE ? ESCAPE '\\')"
            )
            params.extend([like] * 4)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _sighting(row: sqlite3.Row) -> dict:
    city = reference.CITIES.get(row["location"])
    return {
        "id": row["id"],
        "date": row["observed_at"],
        "location": row["location"],
        "region": row["region"],
        "species": row["species"],
        "latin_name": row["latin_name"],
        "coordinates": {"lat": city.lat, "lon": city.lon} if city else None,
    }


def search_sightings(conn: sqlite3.Connection, f: SightingFilter, page: int, page_size: int, sort: str):
    where, params = f.where()
    total = conn.execute(f"SELECT COUNT(*) FROM sightings{where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM sightings{where} ORDER BY {SORTS[sort]} LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return total, [_sighting(r) for r in rows]


def get_sighting(conn: sqlite3.Connection, sighting_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sightings WHERE id = ?", (sighting_id,)).fetchone()
    return _sighting(row) if row else None


def count_by(conn: sqlite3.Connection, f: SightingFilter, group_by: str) -> list[dict]:
    column = GROUP_COLUMNS[group_by]
    where, params = f.where()
    rows = conn.execute(
        f"SELECT {column} AS name, COUNT(*) AS count, MIN(observed_at) AS first, MAX(observed_at) AS last "
        f"FROM sightings{where} GROUP BY {column} ORDER BY count DESC, name ASC",
        params,
    ).fetchall()
    total = sum(r["count"] for r in rows) or 1
    out = []
    for r in rows:
        item = {"name": r["name"], "count": r["count"], "share": round(r["count"] / total, 4),
                "first_sighting": r["first"], "last_sighting": r["last"]}
        if group_by == "city":
            city = reference.CITIES.get(r["name"])
            item["region"] = reference.region_for(r["name"])
            item["coordinates"] = {"lat": city.lat, "lon": city.lon} if city else None
        out.append(item)
    return out


def _next_period(d: date, interval: str) -> date:
    if interval == "day":
        return d + timedelta(days=1)
    if interval == "week":
        return d + timedelta(weeks=1)
    if interval == "month":
        return date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return date(d.year + 1, 1, 1)


def _fill(counts: dict[date, int], interval: str, start: date | None = None, end: date | None = None):
    """Zero-fill gaps so a chart shows 'no sightings' rather than skipping the period."""
    if not counts and not (start and end):
        return []
    cur = start or min(counts)
    last = end or max(counts)
    points = []
    while cur <= last:
        points.append({"period": cur.isoformat(), "count": counts.get(cur, 0)})
        cur = _next_period(cur, interval)
    return points


def trend(conn: sqlite3.Connection, f: SightingFilter, interval: str, group_by: str | None = None) -> dict:
    period = TREND_PERIOD_SQL[interval]
    where, params = f.where()
    if not group_by:
        rows = conn.execute(
            f"SELECT {period} AS p, COUNT(*) AS c FROM sightings{where} GROUP BY p ORDER BY p", params
        ).fetchall()
        return {"points": _fill({date.fromisoformat(r["p"]): r["c"] for r in rows}, interval)}

    column = GROUP_COLUMNS[group_by]
    rows = conn.execute(
        f"SELECT {column} AS g, {period} AS p, COUNT(*) AS c FROM sightings{where} GROUP BY g, p ORDER BY g, p",
        params,
    ).fetchall()
    if not rows:
        return {"series": {}}
    periods = [date.fromisoformat(r["p"]) for r in rows]
    lo, hi = min(periods), max(periods)
    grouped: dict[str, dict[date, int]] = {}
    for r in rows:
        grouped.setdefault(r["g"], {})[date.fromisoformat(r["p"])] = r["c"]
    # Every series shares the same x-axis so they can be plotted together.
    return {"series": {g: _fill(c, interval, lo, hi) for g, c in grouped.items()}}


def monthly_series(conn: sqlite3.Connection, f: SightingFilter) -> list[tuple[date, int]]:
    return [(date.fromisoformat(p["period"]), p["count"]) for p in trend(conn, f, "month")["points"]]


def seasonality(conn: sqlite3.Connection, f: SightingFilter) -> list[dict]:
    where, params = f.where()
    rows = conn.execute(
        f"SELECT CAST(strftime('%m', observed_at) AS INTEGER) AS m, COUNT(*) AS c FROM sightings{where} GROUP BY m",
        params,
    ).fetchall()
    counts = {r["m"]: r["c"] for r in rows}
    total = sum(counts.values()) or 1
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return [{"month": m, "name": names[m - 1], "count": counts.get(m, 0), "share": round(counts.get(m, 0) / total, 4)}
            for m in range(1, 13)]


def summary(conn: sqlite3.Connection, f: SightingFilter) -> dict:
    where, params = f.where()
    row = conn.execute(
        f"SELECT COUNT(*) AS n, MIN(observed_at) AS first, MAX(observed_at) AS last, "
        f"COUNT(DISTINCT location) AS cities, COUNT(DISTINCT species) AS species FROM sightings{where}",
        params,
    ).fetchone()
    by_city = count_by(conn, f, "city")
    by_species = count_by(conn, f, "species")
    return {
        "total_sightings": row["n"],
        "first_sighting": row["first"],
        "last_sighting": row["last"],
        "cities": row["cities"],
        "species": row["species"],
        "top_city": by_city[0] if by_city else None,
        "top_species": by_species[0] if by_species else None,
    }


def latest_runs(conn: sqlite3.Connection, limit: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["warnings"] = json.loads(d["warnings"]) if d["warnings"] else []
        out.append(d)
    return out


def data_quality(conn: sqlite3.Connection) -> dict:
    stored = conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
    last = latest_runs(conn, 1)
    last_run = last[0] if last else None
    reasons = []
    examples = []
    if last_run:
        reasons = [dict(r) for r in conn.execute(
            "SELECT kind, reason, COUNT(*) AS count FROM rejected_records WHERE run_id = ? "
            "GROUP BY kind, reason ORDER BY count DESC", (last_run["id"],))]
        examples = [dict(r) for r in conn.execute(
            "SELECT source_id, kind, reason, payload FROM rejected_records WHERE run_id = ? ORDER BY id LIMIT 20",
            (last_run["id"],))]
    unknown_cities = [r[0] for r in conn.execute(
        "SELECT DISTINCT location FROM sightings WHERE region = ?", (reference.UNKNOWN_REGION,))]
    return {
        "stored_sightings": stored,
        "last_run": last_run,
        "rejections_by_reason": reasons,
        "rejected_examples": examples,
        "unmapped_cities": unknown_cities,
    }

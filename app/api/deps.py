"""Shared FastAPI dependencies: DB connection and the common sighting filters."""

import re
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta

from fastapi import Query, Request

from app import reference
from app.db import connect
from app.errors import APIError
from app.repository import SightingFilter


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.settings.database_path)
    try:
        yield conn
    finally:
        conn.close()


def parse_bound(value: str | None, name: str, *, end: bool) -> datetime | None:
    """Parse a time bound at year, month, day or second precision.

    Start bounds snap to the beginning of the unit; end bounds are inclusive of
    the whole unit (end=2024-03 covers all of March) and returned exclusive.
    """
    if value is None:
        return None
    v = value.strip()
    try:
        if re.fullmatch(r"\d{4}", v):
            lo, hi = datetime(int(v), 1, 1), datetime(int(v) + 1, 1, 1)
        elif re.fullmatch(r"\d{4}-\d{2}", v):
            y, m = map(int, v.split("-"))
            lo = datetime(y, m, 1)
            hi = datetime(y + m // 12, m % 12 + 1, 1)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            lo = datetime.combine(date.fromisoformat(v), datetime.min.time())
            hi = lo + timedelta(days=1)
        else:
            lo = datetime.fromisoformat(v).replace(tzinfo=None, microsecond=0)
            hi = lo + timedelta(seconds=1)
    except ValueError:
        raise APIError(400, "invalid_parameter", f"'{name}' must be YYYY, YYYY-MM, YYYY-MM-DD or an ISO datetime",
                       {"field": name, "value": value}) from None
    return hi if end else lo


def sighting_filters(
    start: str | None = Query(None, description="Inclusive start: YYYY, YYYY-MM, YYYY-MM-DD or ISO datetime",
                              examples=["2023-01-01"]),
    end: str | None = Query(None, description="Inclusive end at the same precisions (end=2023-06 covers June)",
                            examples=["2023-12-31"]),
    location: list[str] | None = Query(None, description="City, case-insensitive. Repeat for several cities.",
                                       examples=["London"]),
    region: list[str] | None = Query(None, description="UK region, e.g. 'North West', 'Scotland'. Repeatable."),
    species: list[str] | None = Query(None, description="Species name, case-insensitive. Repeatable."),
    near_lat: float | None = Query(None, ge=-90, le=90, description="Latitude for a radius search"),
    near_lon: float | None = Query(None, ge=-180, le=180, description="Longitude for a radius search"),
    radius_km: float | None = Query(None, gt=0, le=1500, description="Radius around near_lat/near_lon in km"),
    q: str | None = Query(None, min_length=2, max_length=100,
                          description="Free-text search across city, region, species and latin name"),
) -> SightingFilter:
    start_dt = parse_bound(start, "start", end=False)
    end_dt = parse_bound(end, "end", end=True)
    if start_dt and end_dt and start_dt >= end_dt:
        raise APIError(400, "invalid_range", "'start' must be before 'end'", {"start": start, "end": end})

    geo = [near_lat, near_lon, radius_km]
    near_cities = None
    if any(v is not None for v in geo):
        if any(v is None for v in geo):
            raise APIError(400, "invalid_parameter", "near_lat, near_lon and radius_km must be given together")
        near_cities = reference.cities_within(near_lat, near_lon, radius_km)

    return SightingFilter(
        start=start_dt,
        end=end_dt,
        locations=location or [],
        regions=region or [],
        species=species or [],
        near_cities=near_cities,
        text=q.strip() if q else None,
    )


def describe(f: SightingFilter) -> dict:
    """Echo the applied filters back so clients can see exactly what was counted."""
    out: dict = {}
    if f.start:
        out["start"] = f.start.isoformat()
    if f.end:
        out["end_exclusive"] = f.end.isoformat()
    for key, values in (("location", f.locations), ("region", f.regions), ("species", f.species)):
        if values:
            out[key] = values
    if f.near_cities is not None:
        out["near_cities"] = f.near_cities
    if f.text:
        out["q"] = f.text
    return out

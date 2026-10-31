import math
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query

from app import reference, repository
from app.api.deps import describe, get_db, sighting_filters
from app.api.schemas import Sighting, SightingPage
from app.errors import APIError
from app.repository import SightingFilter

router = APIRouter(prefix="/api/v1", tags=["Sightings"])


@router.get("/sightings", response_model=SightingPage, summary="Search and filter sightings")
def search_sightings(
    f: SightingFilter = Depends(sighting_filters),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    sort: Literal["-date", "date", "location", "species"] = Query("-date", description="'-date' = newest first"),
    conn: sqlite3.Connection = Depends(get_db),
):
    total, items = repository.search_sightings(conn, f, page, page_size, sort)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
        "filters": describe(f),
        "items": items,
    }


@router.get("/sightings/{sighting_id}", response_model=Sighting, summary="Get one sighting")
def get_sighting(sighting_id: str = Path(..., max_length=100), conn: sqlite3.Connection = Depends(get_db)):
    sighting = repository.get_sighting(conn, sighting_id)
    if not sighting:
        raise APIError(404, "not_found", f"No sighting with id '{sighting_id}'")
    return sighting


@router.get("/locations", summary="Cities with sightings, with region and coordinates")
def list_locations(conn: sqlite3.Connection = Depends(get_db)):
    return {"items": repository.count_by(conn, SightingFilter(), "city")}


@router.get("/regions", summary="UK regions and the cities that map to them")
def list_regions(conn: sqlite3.Connection = Depends(get_db)):
    counts = {r["name"]: r["count"] for r in repository.count_by(conn, SightingFilter(), "region")}
    regions: dict[str, list[str]] = {}
    for city in reference.CITIES.values():
        regions.setdefault(city.region, []).append(city.name)
    return {"items": [{"name": name, "cities": sorted(cities), "count": counts.get(name, 0)}
                      for name, cities in sorted(regions.items())]}


@router.get("/species", summary="Tick species with latin names and counts")
def list_species(conn: sqlite3.Connection = Depends(get_db)):
    items = repository.count_by(conn, SightingFilter(), "species")
    for item in items:
        item["latin_name"] = reference.latin_for(item["name"])
    return {"items": items}

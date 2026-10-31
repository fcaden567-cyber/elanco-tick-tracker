import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Query

from app import repository
from app.api.deps import describe, get_db, sighting_filters
from app.api.schemas import GroupCountReport, TrendReport
from app.repository import SightingFilter

router = APIRouter(prefix="/api/v1/reports", tags=["Reports"])


@router.get("/summary", summary="Headline numbers for a dashboard")
def summary(f: SightingFilter = Depends(sighting_filters), conn: sqlite3.Connection = Depends(get_db)):
    return {"filters": describe(f), **repository.summary(conn, f)}


@router.get("/sightings-per-region", response_model=GroupCountReport, response_model_exclude_none=True,
            summary="Number of sightings per region")
def sightings_per_region(
    group_by: Literal["city", "region"] = Query(
        "city", description="'city' = the location in the feed; 'region' = UK region (e.g. Scotland, North West)"
    ),
    f: SightingFilter = Depends(sighting_filters),
    conn: sqlite3.Connection = Depends(get_db),
):
    items = repository.count_by(conn, f, group_by)
    return {"group_by": group_by, "total": sum(i["count"] for i in items), "filters": describe(f), "items": items}


@router.get("/species", response_model=GroupCountReport, response_model_exclude_none=True,
            summary="Number of sightings per species")
def sightings_per_species(f: SightingFilter = Depends(sighting_filters), conn: sqlite3.Connection = Depends(get_db)):
    items = repository.count_by(conn, f, "species")
    return {"group_by": "species", "total": sum(i["count"] for i in items), "filters": describe(f), "items": items}


@router.get("/trends", response_model=TrendReport, summary="Sightings over time (weekly, monthly, ...)")
def trends(
    interval: Literal["day", "week", "month", "year"] = Query("month", description="Weeks start on Monday"),
    group_by: Literal["city", "region", "species"] | None = Query(None, description="Split into one series per group"),
    f: SightingFilter = Depends(sighting_filters),
    conn: sqlite3.Connection = Depends(get_db),
):
    return {"interval": interval, "group_by": group_by, "filters": describe(f),
            **repository.trend(conn, f, interval, group_by)}


@router.get("/seasonality", summary="Sightings by calendar month (when is tick season?)")
def seasonality(f: SightingFilter = Depends(sighting_filters), conn: sqlite3.Connection = Depends(get_db)):
    return {"filters": describe(f), "months": repository.seasonality(conn, f)}

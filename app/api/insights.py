import sqlite3
from dataclasses import replace

from fastapi import APIRouter, Depends, Query

from app import insights, repository
from app.api.deps import describe, get_db, sighting_filters
from app.repository import SightingFilter

router = APIRouter(prefix="/api/v1/insights", tags=["Insights (AI/ML extension)"])


@router.get("/overview", summary="Is tick activity rising, and is there a tick season?")
def overview(f: SightingFilter = Depends(sighting_filters), conn: sqlite3.Connection = Depends(get_db)):
    yearly = {int(p["period"][:4]): p["count"] for p in repository.trend(conn, f, "year")["points"]}
    last = repository.summary(conn, f)["last_sighting"]
    if yearly and last and last[5:7] != "12":
        yearly.pop(max(yearly))  # drop an incomplete final year
    months = [m["count"] for m in repository.seasonality(conn, f)]
    return {
        "filters": describe(f),
        "trend": insights.yearly_trend(yearly),
        "seasonality": insights.seasonality_test(months),
    }


@router.get("/forecast", summary="Forecast monthly sightings with a back-tested model")
def forecast(
    months: int = Query(6, ge=1, le=24, description="How many months ahead to forecast"),
    f: SightingFilter = Depends(sighting_filters),
    conn: sqlite3.Connection = Depends(get_db),
):
    series = repository.monthly_series(conn, f)
    return {"filters": describe(f), **insights.forecast(series, months)}


@router.get("/hotspots", summary="Cities with unusually high recent activity (early-warning)")
def hotspots(
    window_months: int = Query(12, ge=1, le=60, description="Recent window compared against each city's baseline"),
    f: SightingFilter = Depends(sighting_filters),
    conn: sqlite3.Connection = Depends(get_db),
):
    # Every city shares the same calendar so "recent" means the same months everywhere.
    overall = repository.monthly_series(conn, f)
    if not overall:
        return {"filters": describe(f), "items": []}
    city_series = {}
    for city in repository.count_by(conn, f, "city"):
        counts = dict(repository.monthly_series(conn, replace(f, locations=[city["name"]])))
        city_series[city["name"]] = [(d, counts.get(d, 0)) for d, _ in overall]
    return {"filters": describe(f), "window_months": window_months,
            "items": insights.hotspots(city_series, window_months)}


@router.get("/anomalies", summary="Months with significantly more sightings than expected")
def anomalies(
    alpha: float = Query(0.01, gt=0, lt=0.2, description="Significance level for flagging a month"),
    f: SightingFilter = Depends(sighting_filters),
    conn: sqlite3.Connection = Depends(get_db),
):
    return {"filters": describe(f), **insights.anomalies(repository.monthly_series(conn, f), alpha)}

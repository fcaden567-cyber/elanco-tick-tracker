def test_sync_is_idempotent_and_reports_quality(app_client):
    first = app_client.post("/api/v1/ingestion/run").json()
    assert first["status"] == "success"
    assert (first["fetched"], first["inserted"], first["duplicates"], first["rejected"]) == (13, 5, 2, 6)

    second = app_client.post("/api/v1/ingestion/run").json()
    assert (second["inserted"], second["updated"], second["unchanged"]) == (0, 0, 5)

    quality = app_client.get("/api/v1/data-quality").json()
    assert quality["stored_sightings"] == 5
    assert quality["unmapped_cities"] == ["St Albans"]
    assert sum(r["count"] for r in quality["rejections_by_reason"]) == 8


def test_changed_record_is_updated(app_client, upstream):
    app_client.post("/api/v1/ingestion/run")
    upstream.records = [dict(r, location="Leeds") if isinstance(r, dict) and r.get("id") == "a5" else r
                        for r in upstream.records]
    run = app_client.post("/api/v1/ingestion/run").json()
    assert run["updated"] == 1
    assert app_client.get("/api/v1/sightings/a5").json()["location"] == "Leeds"


def test_upstream_outage_keeps_serving_last_good_data(app_client, upstream):
    app_client.post("/api/v1/ingestion/run")
    upstream.bulk_status = 503
    upstream.city_status = 503
    run = app_client.post("/api/v1/ingestion/run").json()
    assert run["status"] == "failed" and "per-city fallback failed" in run["error"]

    resp = app_client.get("/api/v1/sightings")
    assert resp.status_code == 200 and resp.json()["total"] == 5
    assert app_client.get("/health").json()["last_run"]["status"] == "failed"


def test_falls_back_to_per_city_fetch(app_client, upstream):
    upstream.bulk_status = 500
    run = app_client.post("/api/v1/ingestion/run").json()
    assert run["status"] == "success"
    assert run["source"] == "per-city fallback"
    assert any("/city/London" in c for c in upstream.calls)
    assert run["inserted"] >= 1


def test_stale_header_before_first_sync(app_client):
    resp = app_client.get("/api/v1/sightings")
    assert resp.headers["X-Data-Stale"] == "true"
    app_client.post("/api/v1/ingestion/run")
    resp = app_client.get("/api/v1/sightings")
    assert resp.headers["X-Data-Stale"] == "false" and "X-Data-Last-Synced" in resp.headers


def test_admin_token_required_when_configured(tmp_path, upstream):
    from dataclasses import replace

    import httpx
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import make_settings

    app = create_app(replace(make_settings(tmp_path), admin_token="s3cret"),
                     transport=httpx.MockTransport(upstream.handler))
    with TestClient(app) as client:
        assert client.post("/api/v1/ingestion/run").status_code == 401
        assert client.post("/api/v1/ingestion/run", headers={"X-Admin-Token": "s3cret"}).status_code == 200


# -- search -------------------------------------------------------------------


def test_filter_by_time_range_is_inclusive(populated_client):
    body = populated_client.get("/api/v1/sightings", params={"start": "2021-03", "end": "2021-03"}).json()
    assert body["total"] == 8
    assert all(i["date"].startswith("2021-03") for i in body["items"])

    body = populated_client.get("/api/v1/sightings", params={"start": "2021-03-10", "end": "2021-03-10"}).json()
    assert body["total"] == 1


def test_filter_by_location_species_and_text(populated_client):
    get = lambda **p: populated_client.get("/api/v1/sightings", params=p).json()["total"]
    assert get(location="LONDON") == 96
    assert get(location=["London", "Cardiff"]) == 192
    assert get(region="Scotland") == 192
    assert get(location="London", species="marsh tick", start="2022", end="2022") == 12
    assert get(q="glas") == 96
    assert get(q="dermacentor") == 192


def test_radius_search(populated_client):
    # 70 km around Edinburgh reaches Glasgow too, but not London or Cardiff.
    body = populated_client.get("/api/v1/sightings",
                                params={"near_lat": 55.95, "near_lon": -3.19, "radius_km": 70}).json()
    assert sorted(body["filters"]["near_cities"]) == ["Edinburgh", "Glasgow"]
    assert body["total"] == 192


def test_pagination_and_sorting(populated_client):
    page1 = populated_client.get("/api/v1/sightings", params={"page_size": 5, "sort": "date"}).json()
    page2 = populated_client.get("/api/v1/sightings", params={"page_size": 5, "page": 2, "sort": "date"}).json()
    assert page1["pages"] == 77 and len(page1["items"]) == 5
    dates = [i["date"] for i in page1["items"] + page2["items"]]
    assert dates == sorted(dates)
    assert not {i["id"] for i in page1["items"]} & {i["id"] for i in page2["items"]}


def test_bad_parameters_return_consistent_errors(populated_client):
    for params, code, status in [
        ({"start": "2024-13-01"}, "invalid_parameter", 400),
        ({"start": "2024", "end": "2023"}, "invalid_range", 400),
        ({"near_lat": 51.5}, "invalid_parameter", 400),
        ({"page_size": 0}, "invalid_parameter", 422),
        ({"sort": "random"}, "invalid_parameter", 422),
    ]:
        resp = populated_client.get("/api/v1/sightings", params=params)
        assert resp.status_code == status, params
        assert resp.json()["error"]["code"] == code
        assert resp.json()["error"]["request_id"] == resp.headers["X-Request-ID"]

    missing = populated_client.get("/api/v1/sightings/does-not-exist")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


# -- reports ------------------------------------------------------------------


def test_sightings_per_region(populated_client):
    by_city = populated_client.get("/api/v1/reports/sightings-per-region").json()
    assert by_city["total"] == 384
    assert {i["name"]: i["count"] for i in by_city["items"]} == {
        "London": 96, "Glasgow": 96, "Edinburgh": 96, "Cardiff": 96}
    assert by_city["items"][0]["coordinates"]["lat"]

    by_region = populated_client.get("/api/v1/reports/sightings-per-region", params={"group_by": "region"}).json()
    assert by_region["items"][0] == {**by_region["items"][0], "name": "Scotland", "count": 192}


def test_monthly_and_weekly_trends(populated_client):
    monthly = populated_client.get("/api/v1/reports/trends", params={"interval": "month", "start": "2022"}).json()
    assert len(monthly["points"]) == 24
    assert all(p["count"] == 8 for p in monthly["points"])

    weekly = populated_client.get("/api/v1/reports/trends",
                                  params={"interval": "week", "start": "2022-01", "end": "2022-02"}).json()
    periods = [p["period"] for p in weekly["points"]]
    assert periods[0] == "2022-01-10"  # a Monday
    assert any(p["count"] == 0 for p in weekly["points"])  # gaps are zero-filled

    split = populated_client.get("/api/v1/reports/trends", params={"interval": "year", "group_by": "species"}).json()
    assert set(split["series"]) == {"Marsh tick", "Passerine tick"}
    assert [p["count"] for p in split["series"]["Marsh tick"]] == [48, 48, 48, 48]


def test_seasonality_and_summary(populated_client):
    months = populated_client.get("/api/v1/reports/seasonality").json()["months"]
    assert len(months) == 12 and all(m["count"] == 32 for m in months)
    summary = populated_client.get("/api/v1/reports/summary").json()
    assert summary["total_sightings"] == 384 and summary["cities"] == 4


# -- insights -------------------------------------------------------------------


def test_insight_endpoints(populated_client):
    overview = populated_client.get("/api/v1/insights/overview").json()
    assert overview["trend"]["direction"] == "stable"
    assert overview["seasonality"]["significant"] is False

    forecast = populated_client.get("/api/v1/insights/forecast", params={"months": 3}).json()
    assert forecast["available"] and len(forecast["points"]) == 3
    assert forecast["points"][0]["period"] == "2024-01-01"
    assert abs(forecast["points"][0]["predicted"] - 8) < 0.5

    hotspots = populated_client.get("/api/v1/insights/hotspots").json()["items"]
    assert {h["city"] for h in hotspots} == {"London", "Glasgow", "Edinburgh", "Cardiff"}
    assert all(h["risk_level"] == "normal" for h in hotspots)

    assert populated_client.get("/api/v1/insights/anomalies").json()["anomalies"] == []

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

# A deliberately messy feed: one of everything the pipeline has to cope with.
DIRTY_FEED = [
    {"id": "a1", "date": "2023-07-01T10:00:00", "location": "London", "species": "Marsh tick",
     "latinName": "Ixodes apronophorus"},
    # repairable: whitespace, casing, space separator, missing latin name
    {"id": "a2", "date": "2023-07-15 08:30:00", "location": "  manchester ", "species": "marsh TICK",
     "latinName": None},
    # species missing but recoverable from the latin name; timezone suffix
    {"id": "a3", "date": "2023-08-02T12:00:00Z", "location": "Leeds", "species": "", "latinName": "Ixodes canisuga"},
    # duplicate id
    {"id": "a1", "date": "2023-07-01T10:00:00", "location": "London", "species": "Marsh tick",
     "latinName": "Ixodes apronophorus"},
    # same sighting submitted twice under a new id
    {"id": "a4", "date": "2023-07-01T10:00:00", "location": "london", "species": "Marsh tick",
     "latinName": "Ixodes apronophorus"},
    # city alias
    {"id": "a5", "date": "2024-01-10T09:00:00", "location": "Newcastle upon Tyne", "species": "Passerine tick",
     "latinName": "Dermacentor frontalis"},
    # unknown city is kept, region "Unknown"
    {"id": "a6", "date": "2024-02-01T00:00:00", "location": "st albans", "species": "Tree-hole tick",
     "latinName": "Ixodes arboricola"},
    # invalid records
    {"date": "2023-01-01T00:00:00", "location": "London", "species": "Marsh tick"},
    {"id": "b2", "date": "not a date", "location": "London", "species": "Marsh tick"},
    {"id": "b3", "date": "2999-01-01T00:00:00", "location": "London", "species": "Marsh tick"},
    {"id": "b4", "date": "2023-01-01T00:00:00", "location": "  ", "species": "Marsh tick"},
    {"id": "b5", "date": "2023-01-01T00:00:00", "location": "London"},
    "garbage",
]


class FakeUpstream:
    """Programmable stand-in for the Elanco API."""

    def __init__(self, records):
        self.records = records
        self.bulk_status = 200
        self.city_status = 200
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path.endswith("/tick-sightings"):
            if self.bulk_status != 200:
                return httpx.Response(self.bulk_status, json={"error": "boom"})
            return httpx.Response(200, content=json.dumps(self.records))
        if "/tick-sightings/city/" in path:
            if self.city_status != 200:
                return httpx.Response(self.city_status, json={"error": "boom"})
            city = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=[r for r in self.records if isinstance(r, dict)
                                             and r.get("location") == city])
        return httpx.Response(404, json={"error": "Not found"})


def make_settings(tmp_path) -> Settings:
    return Settings(
        database_path=str(tmp_path / "test.db"),
        upstream_base_url="https://upstream.test/data",
        upstream_max_retries=1,
        upstream_backoff_seconds=0,
        upstream_min_interval_seconds=0,
        breaker_failure_threshold=50,
        sync_on_startup=False,
        sync_interval_minutes=0,
    )


@pytest.fixture
def upstream():
    return FakeUpstream(DIRTY_FEED)


@pytest.fixture
def app_client(tmp_path, upstream):
    app = create_app(make_settings(tmp_path), transport=httpx.MockTransport(upstream.handler))
    with TestClient(app) as client:
        yield client


def synthetic_feed(years=(2020, 2021, 2022, 2023)):
    """A clean, larger feed for reporting tests: 2 sightings per city per month."""
    records = []
    cities = ["London", "Glasgow", "Edinburgh", "Cardiff"]
    species = ["Marsh tick", "Passerine tick"]
    n = 0
    for y in years:
        for m in range(1, 13):
            for ci, city in enumerate(cities):
                for k in range(2):
                    n += 1
                    records.append({"id": f"s{n}", "date": f"{y}-{m:02d}-{10 + k + ci:02d}T12:00:00",
                                    "location": city, "species": species[k], "latinName": None})
    return records


@pytest.fixture
def populated_client(tmp_path):
    fake = FakeUpstream(synthetic_feed())
    app = create_app(make_settings(tmp_path), transport=httpx.MockTransport(fake.handler))
    with TestClient(app) as client:
        assert client.post("/api/v1/ingestion/run").json()["status"] == "success"
        yield client

"""Response models - these drive the OpenAPI docs at /docs."""

from pydantic import BaseModel


class Coordinates(BaseModel):
    lat: float
    lon: float


class Sighting(BaseModel):
    id: str
    date: str
    location: str
    region: str
    species: str
    latin_name: str | None
    coordinates: Coordinates | None


class SightingPage(BaseModel):
    total: int
    page: int
    page_size: int
    pages: int
    filters: dict
    items: list[Sighting]


class GroupCount(BaseModel):
    name: str
    count: int
    share: float
    first_sighting: str
    last_sighting: str
    region: str | None = None
    coordinates: Coordinates | None = None


class GroupCountReport(BaseModel):
    group_by: str
    total: int
    filters: dict
    items: list[GroupCount]


class TrendPoint(BaseModel):
    period: str
    count: int


class TrendReport(BaseModel):
    interval: str
    group_by: str | None
    filters: dict
    points: list[TrendPoint] | None = None
    series: dict[str, list[TrendPoint]] | None = None


class IngestionRun(BaseModel):
    id: int
    trigger: str
    status: str
    source: str | None
    started_at: str
    finished_at: str | None
    fetched: int
    inserted: int
    updated: int
    unchanged: int
    repaired: int
    duplicates: int
    rejected: int
    warnings: list[str]
    error: str | None

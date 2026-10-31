"""Application factory. Run with:  uvicorn app.main:create_app --factory"""

import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Request

from app import __version__
from app.api import admin, insights, reports, sightings
from app.config import Settings
from app.db import connect, init_db
from app.errors import register_error_handlers
from app.ingestion import SyncInProgressError, SyncService
from app.upstream import CircuitBreaker, TickApiClient

log = logging.getLogger(__name__)

DESCRIPTION = """
Backend for a UK **tick sightings tracker**. It pulls crowdsourced sightings from the
Elanco tick API, cleans and de-duplicates them into a local database, and serves
search, reporting and statistical insight endpoints.

* **Sightings** – search & filter by time range, city, region, species, distance or free text.
* **Reports** – sightings per region, per species, trends over time, seasonality.
* **Insights** – back-tested forecasts, trend/seasonality significance tests, hotspot early-warning.
* **Data pipeline** – trigger a sync, view run history and data-quality reports.

Responses include `X-Data-Last-Synced`, and `X-Data-Stale: true` when the upstream API
has not been reachable recently (the API keeps serving the last good data).
"""


async def _sync_loop(sync: SyncService, settings: Settings) -> None:
    interval = settings.sync_interval_minutes * 60
    if not settings.sync_on_startup:
        if interval <= 0:
            return
        await asyncio.sleep(interval)
    while True:
        try:
            await asyncio.to_thread(sync.run, "scheduled")
        except SyncInProgressError:
            pass
        except Exception:  # keep the loop alive whatever happens
            log.exception("Scheduled sync crashed")
        if interval <= 0:
            return
        await asyncio.sleep(interval)


def create_app(settings: Settings | None = None, transport: httpx.BaseTransport | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db(settings.database_path)

    client = TickApiClient(
        settings.upstream_base_url,
        timeout=settings.upstream_timeout_seconds,
        max_retries=settings.upstream_max_retries,
        backoff_seconds=settings.upstream_backoff_seconds,
        min_interval_seconds=settings.upstream_min_interval_seconds,
        breaker=CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_reset_seconds),
        transport=transport,
    )
    sync = SyncService(settings.database_path, client)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_sync_loop(sync, settings))
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        client.close()

    app = FastAPI(title="UK Tick Sightings API", version=__version__, description=DESCRIPTION, lifespan=lifespan)
    app.state.settings = settings
    app.state.sync = sync
    register_error_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        last = sync.last_success_at
        if last:
            response.headers["X-Data-Last-Synced"] = last.isoformat() + "Z"
        stale = last is None or datetime.now(timezone.utc).replace(tzinfo=None) - last > timedelta(
            minutes=settings.stale_after_minutes
        )
        response.headers["X-Data-Stale"] = str(stale).lower()
        return response

    @app.get("/health", tags=["General"], summary="Liveness, data freshness and upstream status")
    def health():
        conn = connect(settings.database_path)
        try:
            stored = conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
            last_run = conn.execute("SELECT status, finished_at, error FROM ingestion_runs ORDER BY id DESC LIMIT 1"
                                    ).fetchone()
        finally:
            conn.close()
        return {
            "status": "ok" if stored else "degraded",
            "stored_sightings": stored,
            "last_successful_sync": sync.last_success_at.isoformat() + "Z" if sync.last_success_at else None,
            "last_run": dict(last_run) if last_run else None,
            "sync_running": sync.running,
            "upstream_circuit": client.breaker.state,
        }

    @app.get("/", include_in_schema=False)
    def root():
        return {"name": "UK Tick Sightings API", "version": __version__, "docs": "/docs", "health": "/health"}

    for module in (sightings, reports, insights, admin):
        app.include_router(module.router)
    return app

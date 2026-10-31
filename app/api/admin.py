import asyncio
import secrets
import sqlite3

from fastapi import APIRouter, Depends, Header, Query, Request

from app import repository
from app.api.deps import get_db
from app.api.schemas import IngestionRun
from app.errors import APIError
from app.ingestion import SyncInProgressError

router = APIRouter(prefix="/api/v1", tags=["Data pipeline"])


@router.post("/ingestion/run", response_model=IngestionRun, summary="Pull the latest data from the upstream API now")
async def run_ingestion(request: Request, x_admin_token: str | None = Header(None)):
    expected = request.app.state.settings.admin_token
    if expected and not secrets.compare_digest(x_admin_token or "", expected):
        raise APIError(401, "unauthorised", "A valid X-Admin-Token header is required")
    try:
        return await asyncio.to_thread(request.app.state.sync.run, "manual")
    except SyncInProgressError:
        raise APIError(409, "sync_in_progress", "A sync is already running; try again shortly") from None


@router.get("/ingestion/runs", response_model=list[IngestionRun], summary="History of ingestion runs")
def ingestion_runs(limit: int = Query(20, ge=1, le=200), conn: sqlite3.Connection = Depends(get_db)):
    return repository.latest_runs(conn, limit)


@router.get("/data-quality", summary="What the last sync rejected, repaired or de-duplicated, and why")
def data_quality(conn: sqlite3.Connection = Depends(get_db)):
    return repository.data_quality(conn)

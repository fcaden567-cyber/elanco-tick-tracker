"""Runtime configuration, read from environment variables (12-factor style)."""

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    upstream_base_url: str = "https://dev-task.elancoapps.com/data"
    database_path: str = "data/ticks.db"
    upstream_timeout_seconds: float = 20.0
    upstream_max_retries: int = 4
    upstream_backoff_seconds: float = 0.5
    upstream_min_interval_seconds: float = 0.2
    breaker_failure_threshold: int = 3
    breaker_reset_seconds: float = 300.0
    sync_on_startup: bool = True
    # 0 disables the periodic background refresh.
    sync_interval_minutes: int = 60
    # Responses carry X-Data-Stale when the last good sync is older than this.
    stale_after_minutes: int = 180
    # When set, POST /api/v1/ingestion/run requires a matching X-Admin-Token header.
    admin_token: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            upstream_base_url=os.getenv("UPSTREAM_BASE_URL", cls.upstream_base_url),
            database_path=os.getenv("DATABASE_PATH", cls.database_path),
            upstream_timeout_seconds=float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", cls.upstream_timeout_seconds)),
            upstream_max_retries=int(os.getenv("UPSTREAM_MAX_RETRIES", cls.upstream_max_retries)),
            upstream_backoff_seconds=float(os.getenv("UPSTREAM_BACKOFF_SECONDS", cls.upstream_backoff_seconds)),
            upstream_min_interval_seconds=float(
                os.getenv("UPSTREAM_MIN_INTERVAL_SECONDS", cls.upstream_min_interval_seconds)
            ),
            breaker_failure_threshold=int(os.getenv("BREAKER_FAILURE_THRESHOLD", cls.breaker_failure_threshold)),
            breaker_reset_seconds=float(os.getenv("BREAKER_RESET_SECONDS", cls.breaker_reset_seconds)),
            sync_on_startup=_bool("SYNC_ON_STARTUP", cls.sync_on_startup),
            sync_interval_minutes=int(os.getenv("SYNC_INTERVAL_MINUTES", cls.sync_interval_minutes)),
            stale_after_minutes=int(os.getenv("STALE_AFTER_MINUTES", cls.stale_after_minutes)),
            admin_token=os.getenv("ADMIN_TOKEN") or None,
        )

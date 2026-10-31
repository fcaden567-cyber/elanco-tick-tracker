"""One-off sync from the command line:  python -m app.cli sync"""

import argparse
import json
import logging

from app.config import Settings
from app.db import init_db
from app.ingestion import SyncService
from app.upstream import CircuitBreaker, TickApiClient


def main() -> None:
    parser = argparse.ArgumentParser(description="UK tick sightings data tools")
    parser.add_argument("command", choices=["sync"], help="sync: pull the latest data from the upstream API")
    parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    init_db(settings.database_path)
    client = TickApiClient(
        settings.upstream_base_url,
        timeout=settings.upstream_timeout_seconds,
        max_retries=settings.upstream_max_retries,
        backoff_seconds=settings.upstream_backoff_seconds,
        breaker=CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_reset_seconds),
    )
    try:
        summary = SyncService(settings.database_path, client).run("cli")
    finally:
        client.close()
    print(json.dumps(summary, indent=2))
    raise SystemExit(0 if summary["status"] in {"success", "partial"} else 1)


if __name__ == "__main__":
    main()

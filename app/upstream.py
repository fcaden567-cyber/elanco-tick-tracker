"""Resilient client for the Elanco tick sightings API.

Failure handling, from the inside out:
  1. Timeouts on every request, and a small gap between calls so we don't
     trip the upstream rate limiter ourselves.
  2. Retries with exponential backoff + jitter for transient failures
     (timeouts, connection errors, 429, 5xx). 429s honour Retry-After.
  3. A circuit breaker: after repeated failures we stop calling upstream for a
     cool-down period instead of hammering a service that is already down.
  4. A fallback fetch strategy: if the bulk endpoint fails, we rebuild the
     dataset city by city from /tick-sightings/city/{city}.
If all of that fails the sync is marked failed and the API keeps serving the
last good copy of the data from the local database (flagged as stale).
"""

import logging
import random
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class UpstreamError(Exception):
    """The upstream API could not give us usable data."""


class CircuitOpenError(UpstreamError):
    pass


class CircuitBreaker:
    def __init__(self, failure_threshold: int, reset_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if self._clock() - self._opened_at >= self.reset_seconds:
                return "half-open"  # next call is a trial
            return "open"

    def before_call(self) -> None:
        if self.state == "open":
            raise CircuitOpenError("Upstream circuit is open after repeated failures; skipping call")

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = self._clock()


@dataclass
class FetchResult:
    records: list[Any]
    source: str
    partial: bool = False
    warnings: list[str] = field(default_factory=list)


class TickApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 20.0,
        max_retries: int = 4,
        backoff_seconds: float = 0.5,
        min_interval_seconds: float = 0.2,
        breaker: CircuitBreaker | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "uk-tick-tracker/1.0"},
        )
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.min_interval_seconds = min_interval_seconds
        self.breaker = breaker or CircuitBreaker(failure_threshold=3, reset_seconds=300)
        self._sleep = sleep
        self._last_call = 0.0

    def close(self) -> None:
        self._client.close()

    # -- low level -----------------------------------------------------------

    def _throttle(self) -> None:
        wait = self.min_interval_seconds - (time.monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()

    def _backoff(self, attempt: int) -> float:
        return min(30.0, self.backoff_seconds * 2**attempt) + random.uniform(0, self.backoff_seconds)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        value = resp.headers.get("Retry-After")
        try:
            return min(60.0, float(value)) if value else None
        except ValueError:
            return None

    def get_json(self, path: str) -> Any:
        self.breaker.before_call()
        last_problem = "unknown error"
        for attempt in range(self.max_retries + 1):
            self._throttle()
            delay = self._backoff(attempt)
            try:
                resp = self._client.get(path.lstrip("/"))
            except httpx.TransportError as exc:  # timeouts, DNS, connection resets
                last_problem = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        last_problem = "response was not valid JSON"
                    else:
                        self.breaker.record_success()
                        return data
                elif resp.status_code in RETRYABLE_STATUS:
                    last_problem = f"HTTP {resp.status_code}"
                    delay = self._retry_after(resp) or delay
                else:
                    # 4xx other than the ones above won't fix themselves by retrying.
                    raise UpstreamError(f"GET {path} returned HTTP {resp.status_code}")
            if attempt < self.max_retries:
                log.warning("GET %s failed (%s); retry %d/%d in %.1fs", path, last_problem, attempt + 1,
                            self.max_retries, delay)
                self._sleep(delay)
        self.breaker.record_failure()
        raise UpstreamError(f"GET {path} failed after {self.max_retries + 1} attempts ({last_problem})")

    @staticmethod
    def _expect_list(data: Any, path: str) -> list[Any]:
        if not isinstance(data, list):
            raise UpstreamError(f"GET {path} returned {type(data).__name__}, expected a list of sightings")
        return data

    # -- high level ----------------------------------------------------------

    def fetch_all_sightings(self, fallback_cities: Iterable[str] = ()) -> FetchResult:
        try:
            return FetchResult(self._expect_list(self.get_json("/tick-sightings"), "/tick-sightings"), source="bulk")
        except CircuitOpenError:
            raise
        except UpstreamError as bulk_error:
            cities = sorted(set(fallback_cities))
            if not cities:
                raise
            log.warning("Bulk fetch failed (%s); falling back to per-city fetch for %d cities", bulk_error,
                        len(cities))
            records: list[Any] = []
            warnings = [f"bulk endpoint failed: {bulk_error}"]
            failed = []
            for city in cities:
                path = f"/tick-sightings/city/{quote(city, safe='')}"
                try:
                    records.extend(self._expect_list(self.get_json(path), path))
                except UpstreamError as exc:
                    failed.append(city)
                    warnings.append(f"city '{city}' failed: {exc}")
            if len(failed) == len(cities):
                raise UpstreamError(f"bulk fetch failed ({bulk_error}) and every per-city fallback failed")
            return FetchResult(records, source="per-city fallback", partial=bool(failed), warnings=warnings)

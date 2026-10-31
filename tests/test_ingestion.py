from datetime import datetime

import httpx
import pytest

from app.ingestion import InvalidRecord, clean_records, parse_timestamp
from app.upstream import CircuitBreaker, CircuitOpenError, TickApiClient, UpstreamError
from tests.conftest import DIRTY_FEED

NOW = datetime(2026, 10, 31)


def test_clean_records_validates_repairs_and_dedupes():
    result = clean_records(DIRTY_FEED, now=NOW)
    by_id = {s.id: s for s in result.sightings}

    assert sorted(by_id) == ["a1", "a2", "a3", "a5", "a6"]
    assert result.duplicates == 2
    assert result.invalid == 6

    assert by_id["a2"].location == "Manchester"
    assert by_id["a2"].species == "Marsh tick"
    assert by_id["a2"].latin_name == "Ixodes apronophorus"  # imputed
    assert by_id["a2"].observed_at == "2023-07-15T08:30:00"
    assert by_id["a3"].species == "Fox/badger tick"  # recovered from latin name
    assert by_id["a5"].location == "Newcastle"
    assert by_id["a6"].location == "St Albans" and by_id["a6"].region == "Unknown"
    assert result.repaired >= 3

    reasons = {r.reason for r in result.rejections}
    assert {"missing id", "date is in the future", "missing location", "missing species",
            "record is not a JSON object"} <= reasons


@pytest.mark.parametrize("value, expected", [
    ("2023-07-01T10:00:00", "2023-07-01T10:00:00"),
    ("2023-07-01 10:00:00.123", "2023-07-01T10:00:00"),
    ("2023-07-01T11:00:00+01:00", "2023-07-01T10:00:00"),
    ("01/07/2023 10:00", "2023-07-01T10:00:00"),
    ("2023-07-01", "2023-07-01T00:00:00"),
])
def test_parse_timestamp_formats(value, expected):
    assert parse_timestamp(value).isoformat() == expected


@pytest.mark.parametrize("value", [None, "", "yesterday", 12.5, {"a": 1}])
def test_parse_timestamp_rejects(value):
    with pytest.raises(InvalidRecord):
        parse_timestamp(value)


def _client(handler, sleeps, **kw):
    return TickApiClient("https://upstream.test/data", transport=httpx.MockTransport(handler), max_retries=3,
                         backoff_seconds=0.1, min_interval_seconds=0, sleep=sleeps.append, **kw)


def test_retries_rate_limit_then_succeeds_honouring_retry_after():
    responses = iter([httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(503),
                      httpx.Response(200, json=[])])
    sleeps: list[float] = []
    client = _client(lambda req: next(responses), sleeps)
    assert client.get_json("/tick-sightings") == []
    assert sleeps[0] == 2.0 and len(sleeps) == 2


def test_gives_up_after_max_retries_and_does_not_retry_client_errors():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(500)

    client = _client(handler, [])
    with pytest.raises(UpstreamError, match="after 4 attempts"):
        client.get_json("/tick-sightings")
    assert len(calls) == 4

    calls.clear()
    client = _client(lambda req: calls.append(1) or httpx.Response(400), [])
    with pytest.raises(UpstreamError, match="HTTP 400"):
        client.get_json("/tick-sightings")
    assert len(calls) == 1


def test_timeouts_and_bad_json_are_retried():
    responses = iter([httpx.ReadTimeout("slow"), httpx.Response(200, content=b"<html>"),
                      httpx.Response(200, json=[{"id": "x"}])])

    def handler(req):
        r = next(responses)
        if isinstance(r, Exception):
            raise r
        return r

    assert _client(handler, []).get_json("/tick-sightings") == [{"id": "x"}]


def test_circuit_breaker_opens_and_recovers():
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=60, clock=lambda: now[0])
    breaker.record_failure()
    breaker.before_call()
    breaker.record_failure()
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    now[0] = 61
    assert breaker.state == "half-open"
    breaker.before_call()
    breaker.record_success()
    assert breaker.state == "closed"


def test_non_list_payload_is_an_error():
    client = _client(lambda req: httpx.Response(200, json={"error": "oops"}), [])
    with pytest.raises(UpstreamError, match="expected a list"):
        client.fetch_all_sightings()

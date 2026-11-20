"""Tests for the live and fixture fetchers."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx

from beacon.adapters.clock import FixedClock
from beacon.adapters.fetchers import TRANSPORT_ERROR, FixtureFetcher, HttpFetcher

CLOCK = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
URL = "https://malegislature.gov/Bills/194/H1"


def http(handler, retries: int = 1) -> HttpFetcher:
    return HttpFetcher(
        CLOCK, min_interval=0, retries=retries, transport=httpx.MockTransport(handler)
    )


def test_200_then_conditional_304():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"ETag": '"v1"'})
        return httpx.Response(200, content=b"<html>", headers={"ETag": '"v1"'})

    fetcher = http(handler)
    first = fetcher.fetch(URL, uuid4())
    assert first.body == b"<html>"
    assert first.capture.etag == '"v1"'
    assert "BeaconHillTracker" in seen[0].headers["user-agent"]
    second = fetcher.fetch(URL, uuid4(), prior=first.capture)
    assert second.body is None
    assert second.capture.http_status == 304
    assert second.capture.not_modified_of == first.capture.capture_id
    assert second.capture.content_hash == first.capture.content_hash


def test_retries_server_errors():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, content=b"ok")

    result = http(handler).fetch(URL, uuid4())
    assert len(calls) == 2
    assert result.capture.http_status == 200


def test_transport_error_recorded():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    result = http(handler, retries=0).fetch(URL, uuid4())
    assert result.capture.http_status == TRANSPORT_ERROR
    assert result.body is None
    assert result.capture.content_hash is None


def test_fixture_fetcher_304_on_unchanged():
    fetcher = FixtureFetcher(CLOCK, {URL: b"<html>"})
    first = fetcher.fetch(URL, uuid4())
    assert fetcher.fetch(URL, uuid4(), prior=first.capture).capture.http_status == 304
    fetcher.pages[URL] = b"<html>changed"
    assert fetcher.fetch(URL, uuid4(), prior=first.capture).capture.http_status == 200
    assert fetcher.fetch("https://x/missing", uuid4()).capture.http_status == 404

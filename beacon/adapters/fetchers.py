"""Fetcher implementations: live HTTP and in-memory fixtures."""

import hashlib
import threading
import time
from collections.abc import Mapping
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from beacon.domain.captures import Capture
from beacon.ports.clock import Clock
from beacon.ports.fetcher import FetchResult

USER_AGENT = "BeaconHillTracker/2 (info@beaconhilltracker.org)"
TRANSPORT_ERROR = 0


def _conditional_headers(prior: Capture | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if prior is None:
        return headers
    if prior.etag:
        headers["If-None-Match"] = prior.etag
    if prior.last_modified:
        headers["If-Modified-Since"] = prior.last_modified
    return headers


def _result(
    url: str,
    request_headers: Mapping[str, str],
    run_id: UUID,
    clock: Clock,
    status: int,
    body: bytes | None,
    prior: Capture | None,
    headers: Mapping[str, str],
) -> FetchResult:
    requested_at = clock.now()
    if status == 304 and prior is not None:
        capture = Capture(
            capture_id=uuid4(),
            run_id=run_id,
            url=url,
            request_headers=dict(request_headers),
            requested_at=requested_at,
            http_status=304,
            content_hash=prior.content_hash,
            not_modified_of=prior.capture_id,
            media_type=prior.media_type,
            etag=headers.get("etag", prior.etag),
            last_modified=headers.get("last-modified", prior.last_modified),
        )
        return FetchResult(capture, None)
    ok = status == 200 and body is not None
    capture = Capture(
        capture_id=uuid4(),
        run_id=run_id,
        url=url,
        requested_at=requested_at,
        http_status=status,
        content_hash=hashlib.sha256(body).hexdigest() if ok and body else None,
        media_type=headers.get("content-type"),
        etag=headers.get("etag"),
        last_modified=headers.get("last-modified"),
    )
    return FetchResult(capture, body if ok else None)


class _HostThrottle:
    """Enforces a minimum interval between requests to the same host."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next_slot: dict[str, float] = {}

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, now))
            self._next_slot[host] = slot + self._min_interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class HttpFetcher:
    """Polite live fetcher: per-host throttle, conditional GETs, one retry."""

    def __init__(
        self,
        clock: Clock,
        min_interval: float = 0.5,
        timeout: float = 30.0,
        retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._clock = clock
        self._throttle = _HostThrottle(min_interval)
        self._retries = retries
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            transport=transport,
        )

    def fetch(
        self,
        url: str,
        run_id: UUID,
        prior: Capture | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        extra = dict(headers or {})
        request_headers = extra | _conditional_headers(prior)
        status, body, response_headers = TRANSPORT_ERROR, None, {}
        for attempt in range(self._retries + 1):
            self._throttle.wait(urlsplit(url).netloc)
            try:
                response = self._client.get(url, headers=request_headers)
            except httpx.TransportError:
                status = TRANSPORT_ERROR
            else:
                status, body = response.status_code, response.content
                response_headers = {k.lower(): v for k, v in response.headers.items()}
            if status not in (TRANSPORT_ERROR, 429) and status < 500:
                break
            if attempt < self._retries:
                time.sleep(2**attempt)
        return _result(
            url, extra, run_id, self._clock, status, body, prior, response_headers
        )

    def close(self) -> None:
        """Close the HTTP connection pool."""
        self._client.close()


class FixtureFetcher:
    """Serves canned bodies by URL; answers 304 when content is unchanged."""

    def __init__(self, clock: Clock, pages: Mapping[str, bytes]) -> None:
        self._clock = clock
        self.pages = dict(pages)
        self.requests: list[str] = []

    def fetch(
        self,
        url: str,
        run_id: UUID,
        prior: Capture | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        self.requests.append(url)
        extra = dict(headers or {})
        body = self.pages.get(url)
        if body is None:
            return _result(url, extra, run_id, self._clock, 404, None, prior, {})
        unchanged = (
            prior is not None and prior.content_hash == hashlib.sha256(body).hexdigest()
        )
        status = 304 if unchanged else 200
        return _result(url, extra, run_id, self._clock, status, body, prior, {})

"""Retrieval of web resources."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from beacon.domain.captures import Capture


@dataclass(frozen=True)
class FetchResult:
    """A capture and, unless the server answered 304 or failed, its body."""

    capture: Capture
    body: bytes | None


class Fetcher(Protocol):
    """Fetches a URL, conditionally on a prior capture when given."""

    def fetch(
        self,
        url: str,
        run_id: UUID,
        prior: Capture | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult: ...

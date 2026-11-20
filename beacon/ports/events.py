"""Destination for run events."""

from typing import Protocol

from beacon.domain.events import RunEvent


class EventSink(Protocol):
    """Receives every event; must be safe to call from worker threads."""

    def emit(self, event: RunEvent) -> None: ...

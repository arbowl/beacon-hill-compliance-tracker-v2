"""An event sink that fans run events out to connected browsers."""

import asyncio
import threading

from beacon.domain.events import RunEvent


class BroadcastSink:
    """Delivers each event, as JSON, to every subscribed asyncio queue."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[
            tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]
        ] = []

    def subscribe(self) -> asyncio.Queue[str]:
        """A queue that receives every event from now on; call from the event loop."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        """Stop delivering to a queue."""
        with self._lock:
            self._subscribers = [
                (lp, q) for lp, q in self._subscribers if q is not queue
            ]

    def emit(self, event: RunEvent) -> None:
        payload = event.model_dump_json()
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            if not loop.is_closed():
                loop.call_soon_threadsafe(_offer, queue, payload)


def _offer(queue: asyncio.Queue[str], payload: str) -> None:
    if not queue.full():
        queue.put_nowait(payload)

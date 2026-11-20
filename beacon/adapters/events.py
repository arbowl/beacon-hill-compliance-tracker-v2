"""Event fan-out and sinks: console, JSON lines, store heartbeat, logging."""

import logging
import threading
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO
from uuid import UUID

from beacon.domain.events import (
    CommitteeStarted,
    ItemFailed,
    PlanFinished,
    PlanStarted,
    Progress,
    RunEvent,
    RunFinished,
    RunStarted,
)
from beacon.ports.events import EventSink
from beacon.ports.store import RunRepo

log = logging.getLogger("beacon")
_BAR_WIDTH = 20


class EventBus:
    """Delivers each event to every sink; a failing sink is logged and skipped."""

    def __init__(self, sinks: Sequence[EventSink] = ()) -> None:
        self._sinks = list(sinks)
        self._lock = threading.Lock()

    def emit(self, event: RunEvent) -> None:
        with self._lock:
            for sink in self._sinks:
                try:
                    sink.emit(event)
                except Exception:
                    log.exception("Event sink %s failed", type(sink).__name__)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


class ConsoleSink:
    """A live progress line per stage; periodic plain lines when not a terminal."""

    def __init__(
        self,
        stream: TextIO,
        min_interval: float = 0.5,
        plain_interval: float = 10.0,
    ) -> None:
        self._stream = stream
        self._tty = stream.isatty()
        self._interval = min_interval if self._tty else plain_interval
        self._last_draw = 0.0
        self._started: dict[UUID, float] = {}
        self._outcomes: dict[UUID, Counter[str]] = {}
        self._labels: dict[UUID, str] = {}

    def _write(self, text: str, newline: bool) -> None:
        if self._tty:
            self._stream.write("\r\x1b[2K" + text + ("\n" if newline else ""))
        else:
            self._stream.write(text + "\n")
        self._stream.flush()

    def _line(self, event: Progress) -> str:
        label = self._labels.get(event.run_id, event.stage)
        outcomes = " ".join(
            f"{k}:{v}" for k, v in sorted(self._outcomes[event.run_id].items())
        )
        if not event.total:
            return f"{label} {event.done} done {outcomes}"
        filled = int(_BAR_WIDTH * event.done / event.total)
        bar = "#" * filled + "-" * (_BAR_WIDTH - filled)
        elapsed = time.monotonic() - self._started.get(event.run_id, time.monotonic())
        remaining = (
            elapsed / event.done * (event.total - event.done) if event.done else 0
        )
        pct = 100 * event.done // event.total
        return (
            f"{label} [{bar}] {event.done}/{event.total} {pct}% {outcomes} "
            f"ETA {_duration(remaining)}"
        )

    def emit(self, event: RunEvent) -> None:
        if isinstance(event, CommitteeStarted):
            if event.total > 1:
                self._write(
                    f"Committee {event.position} of {event.total}: "
                    f"{event.committee_id}",
                    newline=True,
                )
            return
        if isinstance(event, PlanFinished):
            if event.committees_done > 1:
                self._write(
                    f"All done: {event.committees_done} committees, {event.status} "
                    f"in {_duration(event.duration_s)}",
                    newline=True,
                )
            return
        if isinstance(event, RunStarted):
            self._started[event.run_id] = time.monotonic()
            self._outcomes[event.run_id] = Counter()
            self._labels[event.run_id] = " ".join(
                p for p in (event.stage, event.scope) if p
            )
            self._write(f"{self._labels[event.run_id]}: started", newline=True)
        elif isinstance(event, Progress):
            self._outcomes.setdefault(event.run_id, Counter())[event.outcome] += 1
            now = time.monotonic()
            finished = event.total is not None and event.done >= event.total
            if finished or now - self._last_draw >= self._interval:
                self._last_draw = now
                self._write(self._line(event), newline=False)
        elif isinstance(event, ItemFailed):
            self._write(f"  failed {event.item}: {event.error}", newline=True)
        elif isinstance(event, RunFinished):
            label = self._labels.pop(event.run_id, event.stage)
            stats = " ".join(f"{k}={v}" for k, v in sorted(event.stats.items()))
            self._write(
                f"{label}: {event.status} in {_duration(event.duration_s)} {stats}",
                newline=True,
            )


class JsonLinesSink:
    """Writes each event as one JSON line to a stream (e.g. stdout for piping)."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, event: RunEvent) -> None:
        self._stream.write(event.model_dump_json() + "\n")
        self._stream.flush()


class JsonLinesFileSink:
    """Appends events to <run_id>.jsonl, or plan events to plan-<plan_id>.jsonl."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def emit(self, event: RunEvent) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        name = (
            f"plan-{event.plan_id}"
            if isinstance(event, PlanStarted | CommitteeStarted | PlanFinished)
            else str(event.run_id)
        )
        path = self._directory / f"{name}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")


class HeartbeatSink:
    """Records progress on the run row, at most once per interval."""

    def __init__(self, runs: RunRepo, interval: float = 5.0) -> None:
        self._runs = runs
        self._interval = interval
        self._last: dict[UUID, float] = {}
        self._outcomes: dict[UUID, Counter[str]] = {}

    def emit(self, event: RunEvent) -> None:
        if isinstance(event, PlanStarted | CommitteeStarted | PlanFinished):
            return
        if isinstance(event, RunFinished):
            self._last.pop(event.run_id, None)
            self._outcomes.pop(event.run_id, None)
            return
        outcomes = self._outcomes.setdefault(event.run_id, Counter())
        if isinstance(event, Progress):
            outcomes[event.outcome] += 1
        now = time.monotonic()
        if isinstance(event, Progress) and now - self._last.get(event.run_id, 0) < (
            self._interval
        ):
            return
        self._last[event.run_id] = now
        progress = dict(outcomes)
        if isinstance(event, Progress):
            progress |= {"done": event.done, "total": event.total or 0}
        self._runs.heartbeat(event.run_id, progress)


class LoggingSink:
    """Mirrors run boundaries and failures into the log."""

    def emit(self, event: RunEvent) -> None:
        if isinstance(event, CommitteeStarted):
            log.info(
                "committee %d of %d: %s",
                event.position,
                event.total,
                event.committee_id,
            )
        elif isinstance(event, RunStarted):
            log.info("%s %s started (%s items)", event.stage, event.scope, event.total)
        elif isinstance(event, ItemFailed):
            log.warning("%s %s failed: %s", event.stage, event.item, event.error)
        elif isinstance(event, RunFinished):
            log.info(
                "%s %s in %.1fs %s",
                event.stage,
                event.status,
                event.duration_s,
                event.stats,
            )

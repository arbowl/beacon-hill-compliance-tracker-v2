"""Starts the saved schedule's run while the control center is open."""

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from beacon.gui.control import PreferencesStore
from beacon.gui.jobs import JobBusyError, JobRequest, JobTrigger
from beacon.ports.clock import Clock

POLL_SECONDS = 30.0

_log = logging.getLogger(__name__)


class AttemptOutcome(StrEnum):
    """What happened when a scheduled time arrived."""

    STARTED = "started"
    SKIPPED = "skipped"


class ScheduledAttempt(BaseModel):
    """The most recent time the schedule came due."""

    model_config = ConfigDict(frozen=True)

    due: datetime
    at: datetime
    outcome: AttemptOutcome
    note: str | None = None


class Scheduler:
    """Checks the clock every POLL_SECONDS and starts a job when one is due.

    A time missed while the process was closed is not made up; one missed while
    the machine slept runs once on waking.
    """

    def __init__(
        self,
        preferences: PreferencesStore,
        start_job: Callable[[JobRequest], None],
        clock: Clock,
    ) -> None:
        self._preferences = preferences
        self._start_job = start_job
        self._clock = clock
        self._lock = threading.Lock()
        self._due = preferences.value.schedule.next_after(clock.now())
        self._last: ScheduledAttempt | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def next_due(self) -> datetime | None:
        with self._lock:
            return self._due

    @property
    def last(self) -> ScheduledAttempt | None:
        with self._lock:
            return self._last

    def reschedule(self) -> None:
        """Recompute the next time from the current schedule."""
        with self._lock:
            self._due = self._preferences.value.schedule.next_after(self._clock.now())

    def tick(self) -> None:
        """Start the scheduled job if its time has come."""
        now = self._clock.now()
        with self._lock:
            due = self._due
        if due is None or now < due:
            return
        preferences = self._preferences.value
        request = JobRequest(
            run=preferences.schedule.request(),
            push=preferences.target.auto,
            trigger=JobTrigger.SCHEDULE,
        )
        try:
            self._start_job(request)
        except JobBusyError as error:
            _log.warning("Scheduled run due %s skipped: %s", due, error)
            attempt = ScheduledAttempt(
                due=due, at=now, outcome=AttemptOutcome.SKIPPED, note=str(error)
            )
        else:
            attempt = ScheduledAttempt(due=due, at=now, outcome=AttemptOutcome.STARTED)
        with self._lock:
            self._last = attempt
            self._due = self._preferences.value.schedule.next_after(now)

    def start(self) -> None:
        """Begin checking in a background thread."""
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="beacon-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop checking and wait for the thread to end."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.tick()
            except Exception:
                _log.exception("Scheduler tick failed")

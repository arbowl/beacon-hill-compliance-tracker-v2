"""Starts runs and pushes from the browser, one at a time, in a background thread."""

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from beacon.adapters.clock import SystemClock
from beacon.adapters.run_lock import RunLock, RunLockedError
from beacon.adapters.site_push import bundle_committees
from beacon.config import Settings
from beacon.domain.events import (
    CommitteeStarted,
    PlanStarted,
    Progress,
    RunEvent,
    RunStarted,
)
from beacon.orchestrator.runner import PushOutcome, RunRequest
from beacon.ports.clock import Clock
from beacon.ports.events import EventSink

RunFunction = Callable[[Settings, RunRequest, Sequence[EventSink]], list[Path]]
PushFunction = Callable[[Sequence[str] | None], PushOutcome]
"""Pushes the named committees' newest bundles, or every committee's if None."""

_log = logging.getLogger(__name__)


class JobStatus(StrEnum):
    """Lifecycle of a browser-started job."""

    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobPhase(StrEnum):
    """The part of a job that is running, or that failed."""

    RUN = "run"
    PUSH = "push"


class JobTrigger(StrEnum):
    """What started a job."""

    MANUAL = "manual"
    SCHEDULE = "schedule"


class JobRequest(BaseModel):
    """A run, a push of the newest bundles, or a run then a push of what it wrote."""

    model_config = ConfigDict(frozen=True)

    run: RunRequest | None = None
    push: bool = False
    trigger: JobTrigger = JobTrigger.MANUAL

    @model_validator(mode="after")
    def _something_to_do(self) -> "JobRequest":
        if self.run is None and not self.push:
            raise ValueError("Give a run, a push, or both")
        if self.push and self.run is not None and self.run.as_of is not None:
            raise ValueError("Results as of an earlier date are not sent to the site")
        return self


class JobProgress(BaseModel):
    """Where a running job is: which committee of how many, and which stage."""

    model_config = ConfigDict(frozen=True)

    committees: tuple[str, ...] = ()
    position: int = 0
    committee_id: str | None = None
    stage: str | None = None
    done: int = 0
    stage_total: int | None = None


class JobState(BaseModel):
    """What the browser shows about the current or last job."""

    model_config = ConfigDict(frozen=True)

    status: JobStatus = JobStatus.IDLE
    request: JobRequest | None = None
    phase: JobPhase | None = None
    error: str | None = None
    written: tuple[str, ...] = ()
    pushed: PushOutcome | None = None
    progress: JobProgress | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobBusyError(RuntimeError):
    """A job is already running, here or in another process."""


class JobManager:
    """Runs one request at a time through the shared runner and pusher."""

    def __init__(
        self,
        settings: Settings,
        sinks: Sequence[EventSink],
        run_function: RunFunction,
        push_function: PushFunction,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._sinks = [_ProgressSink(self), *sinks]
        self._run_function = run_function
        self._push_function = push_function
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._state = JobState()
        self._thread: threading.Thread | None = None

    @property
    def state(self) -> JobState:
        with self._lock:
            return self._state

    def start(self, request: JobRequest) -> None:
        """Begin a job; raises JobBusyError if one is running anywhere."""
        with self._lock:
            if self._state.status == JobStatus.RUNNING:
                raise JobBusyError("A job started from this page is still going")
            try:
                with RunLock(self._settings.data_dir):
                    pass
            except RunLockedError as error:
                raise JobBusyError(str(error)) from error
            self._state = JobState(
                status=JobStatus.RUNNING,
                request=request,
                phase=JobPhase.RUN if request.run else JobPhase.PUSH,
                started_at=self._clock.now(),
            )
            self._thread = threading.Thread(
                target=self._work, args=(request,), name="beacon-job", daemon=True
            )
            self._thread.start()

    def observe(self, event: RunEvent) -> None:
        """Fold a run event into the current job's progress."""
        with self._lock:
            if self._state.status != JobStatus.RUNNING:
                return
            progress = self._state.progress or JobProgress()
            if isinstance(event, PlanStarted):
                progress = JobProgress(committees=event.committees)
            elif isinstance(event, CommitteeStarted):
                progress = progress.model_copy(
                    update={
                        "position": event.position,
                        "committee_id": event.committee_id,
                        "stage": None,
                        "done": 0,
                        "stage_total": None,
                    }
                )
            elif isinstance(event, RunStarted):
                progress = progress.model_copy(
                    update={"stage": event.stage, "done": 0, "stage_total": event.total}
                )
            elif isinstance(event, Progress):
                progress = progress.model_copy(
                    update={"done": event.done, "stage_total": event.total}
                )
            self._state = self._state.model_copy(update={"progress": progress})

    def wait(self, timeout: float | None = None) -> None:
        """Block until the current job finishes (for tests)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _work(self, request: JobRequest) -> None:
        written: list[Path] = []
        pushed: PushOutcome | None = None
        phase = JobPhase.RUN
        error: str | None = None
        try:
            if request.run is not None:
                written = self._run_function(self._settings, request.run, self._sinks)
            committees = None if request.run is None else bundle_committees(written)
            if request.push and (committees is None or committees):
                phase = JobPhase.PUSH
                self._set_phase(phase)
                with RunLock(self._settings.data_dir):
                    pushed = self._push_function(committees)
        except Exception as exc:
            _log.exception("Job %s failed during %s", request, phase)
            error = (str(exc) if phase == JobPhase.PUSH else "") or repr(exc)
        state = JobState(
            status=JobStatus.SUCCEEDED if error is None else JobStatus.FAILED,
            request=request,
            phase=None if error is None else phase,
            error=error,
            written=tuple(str(p) for p in written),
            pushed=pushed,
            started_at=self.state.started_at,
            finished_at=self._clock.now(),
        )
        with self._lock:
            self._state = state

    def _set_phase(self, phase: JobPhase) -> None:
        with self._lock:
            self._state = self._state.model_copy(update={"phase": phase})


class _ProgressSink:
    """Feeds events to a JobManager so its state shows where the job is."""

    def __init__(self, manager: JobManager) -> None:
        self._manager = manager

    def emit(self, event: RunEvent) -> None:
        self._manager.observe(event)

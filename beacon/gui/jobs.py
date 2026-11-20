"""Starts runs from the browser, one at a time, in a background thread."""

import threading
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from beacon.adapters.run_lock import RunLock, RunLockedError
from beacon.config import Settings
from beacon.domain.events import (
    CommitteeStarted,
    PlanStarted,
    Progress,
    RunEvent,
    RunStarted,
)
from beacon.orchestrator import runner
from beacon.orchestrator.runner import RunRequest
from beacon.ports.events import EventSink

RunFunction = Callable[[Settings, RunRequest, Sequence[EventSink]], list[Path]]


class JobStatus(StrEnum):
    """Lifecycle of a browser-started job."""

    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


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
    request: RunRequest | None = None
    error: str | None = None
    written: tuple[str, ...] = ()
    progress: JobProgress | None = None


class JobBusyError(RuntimeError):
    """A job is already running, here or in another process."""


class JobManager:
    """Runs one request at a time through the shared runner."""

    def __init__(
        self,
        settings: Settings,
        sinks: Sequence[EventSink],
        run_function: RunFunction = runner.run,
    ) -> None:
        self._settings = settings
        self._sinks = [_ProgressSink(self), *sinks]
        self._run_function = run_function
        self._lock = threading.Lock()
        self._state = JobState()
        self._thread: threading.Thread | None = None

    @property
    def state(self) -> JobState:
        with self._lock:
            return self._state

    def start(self, request: RunRequest) -> None:
        """Begin a job; raises JobBusyError if one is running anywhere."""
        with self._lock:
            if self._state.status == JobStatus.RUNNING:
                raise JobBusyError("A run started from this page is still going")
            try:
                with RunLock(self._settings.data_dir):
                    pass
            except RunLockedError as error:
                raise JobBusyError(str(error)) from error
            self._state = JobState(status=JobStatus.RUNNING, request=request)
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

    def _work(self, request: RunRequest) -> None:
        try:
            written = self._run_function(self._settings, request, self._sinks)
        except Exception as error:
            state = JobState(
                status=JobStatus.FAILED, request=request, error=repr(error)
            )
        else:
            state = JobState(
                status=JobStatus.SUCCEEDED,
                request=request,
                written=tuple(str(p) for p in written),
            )
        with self._lock:
            self._state = state


class _ProgressSink:
    """Feeds events to a JobManager so its state shows where the job is."""

    def __init__(self, manager: JobManager) -> None:
        self._manager = manager

    def emit(self, event: RunEvent) -> None:
        self._manager.observe(event)

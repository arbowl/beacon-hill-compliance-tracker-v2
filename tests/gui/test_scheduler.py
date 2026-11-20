"""The scheduler starts the saved run when due, once, and records what happened."""

from datetime import datetime, time, timedelta
from pathlib import Path

from pydantic import SecretStr

from beacon.adapters.clock import FixedClock
from beacon.domain.local_time import LEGISLATURE_TZ
from beacon.gui.control import (
    ControlPreferences,
    PreferencesStore,
    PushTarget,
    Schedule,
)
from beacon.gui.jobs import JobBusyError, JobRequest, JobTrigger
from beacon.gui.scheduler import AttemptOutcome, Scheduler


def _eastern(*args: int) -> datetime:
    return datetime(*args, tzinfo=LEGISLATURE_TZ)


START = _eastern(2026, 11, 20, 0, 30)


class Starter:
    def __init__(self, busy: bool = False) -> None:
        self.busy = busy
        self.started: list[JobRequest] = []

    def __call__(self, request: JobRequest) -> None:
        if self.busy:
            raise JobBusyError("Another beacon run is using data")
        self.started.append(request)


def _scheduler(tmp_path: Path, starter: Starter, auto: bool = False):
    store = PreferencesStore(tmp_path / "control.json")
    store.replace(
        ControlPreferences(
            schedule=Schedule(enabled=True, at=time(1, 0), committees=("J45",)),
            target=PushTarget(url="https://a.example", key=SecretStr("k"), auto=auto),
        )
    )
    clock = FixedClock(START)
    return Scheduler(store, starter, clock), clock, store


def test_starts_once_when_due_then_waits_for_the_next_day(tmp_path: Path):
    starter = Starter()
    scheduler, clock, _ = _scheduler(tmp_path, starter, auto=True)
    scheduler.tick()
    assert starter.started == []
    clock.current = _eastern(2026, 11, 20, 1, 0, 20)
    scheduler.tick()
    scheduler.tick()
    (request,) = starter.started
    assert request.trigger == JobTrigger.SCHEDULE and request.push
    assert request.run is not None and request.run.committees == ("J45",)
    assert scheduler.next_due == _eastern(2026, 11, 21, 1, 0)
    last = scheduler.last
    assert last is not None and last.outcome == AttemptOutcome.STARTED


def test_missed_while_asleep_runs_once_on_waking(tmp_path: Path):
    starter = Starter()
    scheduler, clock, _ = _scheduler(tmp_path, starter)
    clock.current = START + timedelta(days=3)
    scheduler.tick()
    assert len(starter.started) == 1
    due = scheduler.next_due
    assert due is not None and due > clock.current


def test_busy_data_directory_skips_and_records_why(tmp_path: Path):
    scheduler, clock, _ = _scheduler(tmp_path, Starter(busy=True))
    clock.current = _eastern(2026, 11, 20, 1, 0)
    scheduler.tick()
    last = scheduler.last
    assert last is not None and last.outcome == AttemptOutcome.SKIPPED
    assert last.note and "Another beacon run" in last.note
    assert scheduler.next_due == _eastern(2026, 11, 21, 1, 0)


def test_reschedule_follows_saved_changes(tmp_path: Path):
    starter = Starter()
    scheduler, clock, store = _scheduler(tmp_path, starter)
    later = store.value.schedule.model_copy(update={"at": time(3, 15)})
    store.replace(store.value.model_copy(update={"schedule": later}))
    scheduler.reschedule()
    due = scheduler.next_due
    assert due is not None and due.astimezone(LEGISLATURE_TZ).time() == time(3, 15)
    off = later.model_copy(update={"enabled": False})
    store.replace(store.value.model_copy(update={"schedule": off}))
    scheduler.reschedule()
    clock.current = START + timedelta(days=1)
    scheduler.tick()
    assert scheduler.next_due is None and starter.started == []

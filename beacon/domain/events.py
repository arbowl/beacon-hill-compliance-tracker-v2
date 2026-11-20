"""Structured events describing a run as it happens."""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class EventKind(StrEnum):
    """Discriminator for run and plan events."""

    PLAN_STARTED = "plan_started"
    COMMITTEE_STARTED = "committee_started"
    PLAN_FINISHED = "plan_finished"
    RUN_STARTED = "run_started"
    PROGRESS = "progress"
    ITEM_FAILED = "item_failed"
    RUN_FINISHED = "run_finished"


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    stage: str
    at: AwareDatetime


class RunStarted(_Event):
    """A stage began; total is the expected item count when known."""

    kind: Literal[EventKind.RUN_STARTED] = EventKind.RUN_STARTED
    scope: str | None = None
    total: int | None = None


class Progress(_Event):
    """One item finished with an outcome such as changed, unchanged, or failed."""

    kind: Literal[EventKind.PROGRESS] = EventKind.PROGRESS
    done: int
    total: int | None
    item: str
    outcome: str


class ItemFailed(_Event):
    """One item failed; the run continues."""

    kind: Literal[EventKind.ITEM_FAILED] = EventKind.ITEM_FAILED
    item: str
    error: str


class RunFinished(_Event):
    """A stage ended."""

    kind: Literal[EventKind.RUN_FINISHED] = EventKind.RUN_FINISHED
    status: str
    stats: dict[str, int]
    duration_s: float


class _PlanEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_id: UUID
    at: AwareDatetime


class PlanStarted(_PlanEvent):
    """A request began: its stages, and the committees it will cover in order."""

    kind: Literal[EventKind.PLAN_STARTED] = EventKind.PLAN_STARTED
    stages: tuple[str, ...]
    committees: tuple[str, ...]


class CommitteeStarted(_PlanEvent):
    """The request moved on to its next committee (position counts from 1)."""

    kind: Literal[EventKind.COMMITTEE_STARTED] = EventKind.COMMITTEE_STARTED
    committee_id: str
    position: int
    total: int


class PlanFinished(_PlanEvent):
    """The request ended."""

    kind: Literal[EventKind.PLAN_FINISHED] = EventKind.PLAN_FINISHED
    status: str
    committees_done: int
    duration_s: float


PLAN_EVENTS = (PlanStarted, CommitteeStarted, PlanFinished)

RunEvent = Annotated[
    RunStarted
    | Progress
    | ItemFailed
    | RunFinished
    | PlanStarted
    | CommitteeStarted
    | PlanFinished,
    Field(discriminator="kind"),
]

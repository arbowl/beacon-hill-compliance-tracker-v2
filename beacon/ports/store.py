"""Persistence of runs, captures, inventory, observations, and evaluations."""

from collections.abc import Iterator, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from beacon.domain.captures import Capture
from beacon.domain.evaluation import TenureEvaluation
from beacon.domain.inventory import BillRecord, CommitteeRecord
from beacon.domain.observations import Observation, ObservationKind


class Stage(StrEnum):
    """A pipeline stage that opens runs."""

    COLLECT = "collect"
    SCRAPE = "scrape"
    REEXTRACT = "reextract"
    EVALUATE = "evaluate"
    REPORT = "report"


class RunStatus(StrEnum):
    """Lifecycle of a run."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunRecord(BaseModel):
    """One row of the runs table."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    stage: Stage
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None
    heartbeat_at: datetime | None
    stats: dict[str, int]
    progress: dict[str, int]
    scope: str | None = None


class RunRepo(Protocol):
    def start(
        self,
        stage: Stage,
        code_version: str,
        config_hash: str,
        scope: str | None = None,
    ) -> UUID: ...

    def finish(
        self, run_id: UUID, status: RunStatus, stats: dict[str, int]
    ) -> None: ...

    def heartbeat(self, run_id: UUID, progress: dict[str, int]) -> None: ...

    def progress(self, run_id: UUID) -> tuple[dict[str, int], datetime | None]: ...

    def status(self, run_id: UUID) -> RunStatus: ...

    def stats(self, run_id: UUID) -> dict[str, int]: ...

    def recent(self, limit: int = 50) -> list[RunRecord]: ...


class CaptureRepo(Protocol):
    def add(self, capture: Capture) -> None: ...

    def get(self, capture_id: UUID) -> Capture: ...

    def latest_for(self, url: str) -> Capture | None: ...

    def iter_with_content(self, url_prefix: str = "") -> Iterator[Capture]: ...


class InventoryRepo(Protocol):
    def upsert_committee(self, committee: CommitteeRecord) -> None: ...

    def upsert_bill(self, bill: BillRecord) -> None: ...

    def link(
        self, session: int, committee_id: str, bill_id: str, seen_at: datetime
    ) -> None: ...

    def committee(self, session: int, committee_id: str) -> CommitteeRecord | None: ...

    def committees(self, session: int) -> list[CommitteeRecord]: ...

    def bills_for(self, session: int, committee_id: str) -> list[BillRecord]: ...


class ObservationRepo(Protocol):
    def append_many(self, observations: Sequence[Observation]) -> None: ...

    def for_bill(
        self, session: int, bill_id: str, kind: ObservationKind | None = None
    ) -> list[Observation]: ...

    def exists(
        self, capture_id: UUID, extractor_id: str, extractor_version: int
    ) -> bool: ...

    def of_kind(self, kind: ObservationKind) -> list[Observation]: ...

    def get(self, observation_id: UUID) -> Observation: ...


class EvaluationRepo(Protocol):
    def append_many(self, evaluations: Sequence[TenureEvaluation]) -> None: ...

    def for_run(self, run_id: UUID) -> list[TenureEvaluation]: ...

    def latest_run(
        self, session: int, committee_id: str, ruleset_id: str
    ) -> UUID | None: ...


class Store(Protocol):
    """All repositories over one transactional backend."""

    @property
    def runs(self) -> RunRepo: ...

    @property
    def captures(self) -> CaptureRepo: ...

    @property
    def inventory(self) -> InventoryRepo: ...

    @property
    def observations(self) -> ObservationRepo: ...

    @property
    def evaluations(self) -> EvaluationRepo: ...

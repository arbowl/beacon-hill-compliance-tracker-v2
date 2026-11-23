"""Contract for reporters."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from beacon.domain.evaluation import NoRequirements, TenureEvaluation
from beacon.domain.evidence import EvidenceRef
from beacon.domain.inventory import BillRecord


@dataclass(frozen=True)
class CommitteeOutput:
    """A document the committee produced, with the bills it came from."""

    bill_id: str
    title: str | None
    originated_on: date
    source_bills: tuple[str, ...]


class EvidenceSource(StrEnum):
    """Where a piece of evidence was read."""

    HISTORY = "Bill history"
    SEARCH = "Document search"
    DOCUMENT = "Document"
    HEARING = "Hearing page"
    COMMITTEE_DOCUMENTS = "Committee Documents tab"
    LISTING = "Committee bill list"


@dataclass(frozen=True)
class EvidenceItem:
    """An evidence reference resolved to a dated line of source text and a link."""

    source: EvidenceSource
    occurred_on: date | None
    text: str
    url: str | None


@dataclass(frozen=True)
class ReportInput:
    """One committee's evaluations from one run, with its inventory."""

    session: int
    committee_id: str
    committee_name: str | None
    evaluations: Sequence[TenureEvaluation]
    bills: Sequence[BillRecord]
    committee_output: Sequence[CommitteeOutput] = ()
    evidence: Mapping[EvidenceRef, EvidenceItem] = field(default_factory=dict)
    committee_url: str | None = None
    no_requirements: Sequence[NoRequirements] = ()


@dataclass(frozen=True)
class RunIdentity:
    """The evaluation run a report covers."""

    run_id: UUID
    ruleset_id: str
    ruleset_version: str
    as_of: datetime


def run_identity(report: ReportInput) -> RunIdentity | None:
    """The one run behind a report's evaluations and tenures owing nothing; None
    if it has neither. Raises ValueError if they span runs."""
    runs = {
        RunIdentity(e.run_id, e.ruleset_id, e.ruleset_version, e.as_of)
        for e in report.evaluations
    } | {
        RunIdentity(n.run_id, n.ruleset_id, n.ruleset_version, n.as_of)
        for n in report.no_requirements
    }
    if len(runs) > 1:
        raise ValueError(f"{report.committee_id}: report spans {len(runs)} runs")
    return next(iter(runs), None)


class Reporter(Protocol):
    """Turns evaluations into some output; returns what it wrote."""

    def write(self, report: ReportInput, out_dir: Path) -> list[Path]: ...

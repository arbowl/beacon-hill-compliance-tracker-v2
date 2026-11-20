"""Contract for reporters."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from beacon.domain.evaluation import TenureEvaluation
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


class Reporter(Protocol):
    """Turns evaluations into some output; returns what it wrote."""

    def write(self, report: ReportInput, out_dir: Path) -> list[Path]: ...

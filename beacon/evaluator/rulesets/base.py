"""Contracts every ruleset implements."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from beacon.domain.evaluation import ComplianceLabel, RequirementResult
from beacon.domain.evidence import EvidenceRef
from beacon.domain.provenance import ClauseRef
from beacon.evaluator.facts import BillFacts, Tenure


@dataclass(frozen=True)
class Obligation:
    """A duty a requirement imposes on one tenure."""

    obligation_id: str
    requirement_id: str
    tenure_id: str
    triggered_by: tuple[EvidenceRef, ...]


class Requirement(Protocol):
    """One requirement within a ruleset."""

    @property
    def requirement_id(self) -> str: ...

    @property
    def clauses(self) -> tuple[ClauseRef, ...]: ...

    def obligations(self, facts: BillFacts, tenure: Tenure) -> list[Obligation]: ...

    def check(
        self, obligation: Obligation, facts: BillFacts, tenure: Tenure, as_of: datetime
    ) -> RequirementResult: ...


@dataclass(frozen=True)
class Ruleset:
    """A versioned set of requirements and how they roll up."""

    ruleset_id: str
    version: str
    requirements: tuple[Requirement, ...]
    roll_up: Callable[[Sequence[RequirementResult]], ComplianceLabel]

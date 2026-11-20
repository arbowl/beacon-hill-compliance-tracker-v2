"""Compliance determinations produced by the evaluator."""

from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from beacon.domain.evidence import EvidenceRef
from beacon.domain.ids import BillKey
from beacon.domain.provenance import ClauseRef


class RequirementStatus(StrEnum):
    """Outcome of one requirement."""

    MET = "Met"
    UNMET = "Unmet"
    PROVISIONAL = "Provisional"


class ProvisionalReason(StrEnum):
    """Why a requirement is neither met nor unmet."""

    NOT_YET_DUE = "not_yet_due"
    AMBIGUOUS_TIMING = "ambiguous_timing"
    MISSING_DATA = "missing_data"
    AWAITING_REVIEW = "awaiting_review"


class ComplianceLabel(StrEnum):
    """Outcome for a committee tenure."""

    COMPLIANT = "Compliant"
    NON_COMPLIANT = "Non-Compliant"
    PROVISIONAL = "Provisional"


class StepRole(StrEnum):
    """Machine-readable meaning of a derivation step."""

    TRIGGER = "trigger"
    BASE_DUE = "base_due"
    EXTENSION = "extension"
    EXTENSION_IGNORED = "extension_ignored"
    CAP = "cap"
    TOLERANCE = "tolerance"


class DerivationStep(BaseModel):
    """One step in computing a requirement's outcome."""

    model_config = ConfigDict(frozen=True)

    description: str
    value: date | None = None
    clause: ClauseRef | None = None
    role: StepRole | None = None


class ClauseCitation(BaseModel):
    """A clause relied upon, pinned to the text it had at evaluation time."""

    model_config = ConfigDict(frozen=True)

    ref: ClauseRef
    text_hash: str


class RequirementResult(BaseModel):
    """Outcome of one obligation."""

    model_config = ConfigDict(frozen=True)

    requirement_id: str
    obligation_id: str
    status: RequirementStatus
    reason: ProvisionalReason | None = None
    detail: str
    due: date | None = None
    resolves_on: date | None = None
    completed_on: date | None = None
    derivation: tuple[DerivationStep, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    clauses: tuple[ClauseCitation, ...] = ()

    @model_validator(mode="after")
    def _reason_iff_provisional(self) -> "RequirementResult":
        provisional = self.status == RequirementStatus.PROVISIONAL
        if provisional != (self.reason is not None):
            raise ValueError("reason is required if and only if status is Provisional")
        return self


class TenureEvaluation(BaseModel):
    """All requirement outcomes for one committee tenure at one moment."""

    model_config = ConfigDict(frozen=True)

    evaluation_id: UUID
    run_id: UUID
    bill: BillKey
    committee_id: str
    tenure_id: str
    ruleset_id: str
    ruleset_version: str
    as_of: AwareDatetime
    label: ComplianceLabel
    results: tuple[RequirementResult, ...]
    input_fingerprint: str


def default_roll_up(results: Sequence[RequirementResult]) -> ComplianceLabel:
    """Any unmet -> Non-Compliant; all met -> Compliant; otherwise Provisional."""
    statuses = {r.status for r in results}
    if RequirementStatus.UNMET in statuses:
        return ComplianceLabel.NON_COMPLIANT
    if statuses == {RequirementStatus.MET}:
        return ComplianceLabel.COMPLIANT
    return ComplianceLabel.PROVISIONAL

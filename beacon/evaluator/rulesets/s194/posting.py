"""Posting requirements: committee votes (JR 1D para 14), summaries (JR 1B para 4)."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from beacon.domain.evaluation import (
    DerivationStep,
    ProvisionalReason,
    RequirementResult,
    RequirementStatus,
    StepRole,
)
from beacon.domain.ids import Chamber
from beacon.domain.local_time import LEGISLATURE_TZ, local_date
from beacon.domain.observations import DocumentKind, PublicBasis
from beacon.domain.provenance import ClauseRef, RuleSource
from beacon.evaluator.facts import BillFacts, DocumentEvidence, Tenure
from beacon.evaluator.rulesets.base import Obligation
from beacon.evaluator.rulesets.s194.notice import RULES_EFFECTIVE
from beacon.evaluator.rulesets.s194.requirements import S194Requirement

JR1D_P14 = ClauseRef(RuleSource.JOINT, "1D", 14)
HR17B_P1 = ClauseRef(RuleSource.HOUSE, "17B", 1)
_VOTE_CLAUSE = {Chamber.JOINT: JR1D_P14, Chamber.HOUSE: HR17B_P1}
VOTE_RULES_EFFECTIVE = {
    Chamber.HOUSE: date(2025, 2, 25),
    Chamber.JOINT: RULES_EFFECTIVE,
}
"""When each chamber's vote-posting rule took effect: the House adopted its rules
on February 25, 2025, and the joint rules were adopted on June 26, 2025. Votes
taken earlier are exempt, as hearings announced before the joint rules are."""
JR1B_P4 = ClauseRef(RuleSource.JOINT, "1B", 4)
BILLS_IN_THE_THIRD_READING = "H36"
"""Examines and corrects bills ordered to a third reading (House Rule 22), reporting
changes as amendments; it takes no vote on the legislation for HR 17B to record."""
VOTE_WINDOW = timedelta(hours=48)
LATE_TOLERANCE = timedelta(days=3)
_BASIS_TEXT = {PublicBasis.SITE_POSTED: "posted", PublicBasis.FILE_CREATED: "created"}
_AUTOMATIC_STUDY = re.compile(
    r"under\s+(?:House\s+Rule\s+27|Joint\s+Rule\s+10|JR\s?10)\b", re.I
)


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, LEGISLATURE_TZ)


def _end_of(day: date) -> datetime:
    return datetime.combine(day, time(23, 59, 59), LEGISLATURE_TZ)


@dataclass(frozen=True)
class _Window:
    due: datetime
    late_after: datetime
    steps: tuple[DerivationStep, ...]


def _tolerance_step(late_after: datetime) -> DerivationStep:
    return DerivationStep(
        description=(
            "Tracker policy: Unmet only when posting is shown to be 3 or more days "
            f"late, i.e. after {local_date(late_after)}"
        ),
        value=local_date(late_after),
        role=StepRole.TOLERANCE,
    )


def _judge(
    requirement_id: str,
    obligation: Obligation,
    window: _Window,
    evidence: DocumentEvidence | None,
    as_of: datetime,
    label: str,
) -> RequirementResult:
    """Met if present and not shown late; Unmet if shown 3+ days late or absent."""
    steps = [*window.steps, _tolerance_step(window.late_after)]
    status = RequirementStatus.UNMET
    reason: ProvisionalReason | None = None
    resolves_on: date | None = None
    completed_on: date | None = None
    refs = evidence.evidence if evidence else ()
    if evidence is None or (
        evidence.first_seen_at is None and evidence.absent_before is None
    ):
        status = RequirementStatus.PROVISIONAL
        if as_of <= window.late_after:
            reason, resolves_on = (
                ProvisionalReason.NOT_YET_DUE,
                local_date(window.late_after),
            )
            detail = f"No search for the {label} yet; due {local_date(window.due)}"
        else:
            reason = ProvisionalReason.MISSING_DATA
            detail = f"No complete search for the {label} has been recorded"
    elif evidence.first_seen_at is not None:
        completed_on = local_date(evidence.first_seen_at)
        bounds = [b for b in (evidence.absent_before, evidence.public_from) if b]
        earliest = max(bounds, default=None)
        if earliest is not None and earliest > window.late_after:
            if earliest == evidence.absent_before:
                source = "absent"
            else:
                assert evidence.public_basis is not None
                source = _BASIS_TEXT[evidence.public_basis]
            detail = (
                f"The {label} was {source} on {local_date(earliest)}, more than 3 days "
                f"after the {local_date(window.due)} deadline"
            )
        else:
            status = RequirementStatus.MET
            if evidence.first_seen_at <= window.due:
                detail = f"The {label} was seen by {local_date(window.due)} (verified)"
            else:
                detail = (
                    f"The {label} was first seen {completed_on}; not shown to be late "
                    "(presumed timely)"
                )
    elif evidence.absent_before is not None and evidence.absent_before > (
        window.late_after
    ):
        detail = f"No {label} found as of {local_date(evidence.absent_before)}"
    elif as_of <= window.late_after:
        status = RequirementStatus.PROVISIONAL
        reason = ProvisionalReason.NOT_YET_DUE
        resolves_on = local_date(window.late_after)
        detail = f"No {label} found yet; due {local_date(window.due)}"
    else:
        status = RequirementStatus.PROVISIONAL
        reason = ProvisionalReason.MISSING_DATA
        detail = f"No complete search for the {label} since the tolerance passed"
    return RequirementResult(
        requirement_id=requirement_id,
        obligation_id=obligation.obligation_id,
        status=status,
        reason=reason,
        detail=detail,
        due=local_date(window.due),
        resolves_on=resolves_on,
        completed_on=completed_on,
        derivation=tuple(steps),
        evidence=(*obligation.triggered_by, *refs),
    )


def _obligation(requirement_id: str, tenure: Tenure) -> Obligation:
    return Obligation(
        obligation_id=f"{tenure.tenure_id}:{requirement_id}",
        requirement_id=requirement_id,
        tenure_id=tenure.tenure_id,
        triggered_by=(tenure.referral,),
    )


def _chamber(tenure: Tenure) -> Chamber:
    return Chamber.from_prefix(tenure.committee_id[0])


@dataclass(frozen=True)
class VotesPosted:
    """Joint (JR 1D) and House (HR 17B) committee votes: posted within 48 hours."""

    requirement_id: str = S194Requirement.VOTES_POSTED.value
    clauses: tuple[ClauseRef, ...] = (JR1D_P14, HR17B_P1)

    def obligations(self, facts: BillFacts, tenure: Tenure) -> list[Obligation]:
        """Owed once the committee voted: a vote record, or a non-automatic report."""
        if (
            _chamber(tenure) not in _VOTE_CLAUSE
            or tenure.committee_id == BILLS_IN_THE_THIRD_READING
        ):
            return []
        evidence = facts.document(DocumentKind.VOTES, tenure.committee_id)
        voted = evidence is not None and evidence.vote_date is not None
        report = tenure.report
        reported = report is not None and not _AUTOMATIC_STUDY.search(report.raw_text)
        return [_obligation(self.requirement_id, tenure)] if voted or reported else []

    def awaits(self, facts: BillFacts, tenure: Tenure) -> bool:
        """An open tenure before a committee whose votes are recorded owes a vote
        record once the committee reports."""
        return (
            tenure.report is None
            and tenure.discharged_on is None
            and _chamber(tenure) in _VOTE_CLAUSE
            and tenure.committee_id != BILLS_IN_THE_THIRD_READING
        )

    def check(
        self, obligation: Obligation, facts: BillFacts, tenure: Tenure, as_of: datetime
    ) -> RequirementResult:
        """Judge the vote record's posting against the vote (or report) date."""
        evidence = facts.document(DocumentKind.VOTES, tenure.committee_id)
        if evidence is not None and evidence.vote_date is not None:
            voted_on, basis = evidence.vote_date, "Vote taken"
        else:
            assert tenure.report is not None
            voted_on, basis = tenure.report.reported_on, "Vote date unknown; reported"
        effective = VOTE_RULES_EFFECTIVE[_chamber(tenure)]
        if voted_on < effective:
            return RequirementResult(
                requirement_id=self.requirement_id,
                obligation_id=obligation.obligation_id,
                status=RequirementStatus.MET,
                detail=(
                    f"Voted {voted_on}, before the posting rule took effect "
                    f"{effective}: exempt"
                ),
                derivation=(
                    DerivationStep(
                        description=basis, value=voted_on, role=StepRole.TRIGGER
                    ),
                    DerivationStep(
                        description=(
                            f"Vote posting rule in effect for votes from {effective}; "
                            "earlier votes are exempt"
                        ),
                        value=effective,
                        clause=_VOTE_CLAUSE[_chamber(tenure)],
                        role=StepRole.TOLERANCE,
                    ),
                ),
                evidence=(
                    *obligation.triggered_by,
                    *(evidence.evidence if evidence else ()),
                ),
            )
        due = _end_of(voted_on) + VOTE_WINDOW
        window = _Window(
            due=due,
            late_after=_end_of(voted_on) + LATE_TOLERANCE,
            steps=(
                DerivationStep(
                    description=basis, value=voted_on, role=StepRole.TRIGGER
                ),
                DerivationStep(
                    description="Roll call record due on the website within 48 hours",
                    value=local_date(due),
                    clause=_VOTE_CLAUSE[_chamber(tenure)],
                    role=StepRole.BASE_DUE,
                ),
            ),
        )
        return _judge(
            self.requirement_id, obligation, window, evidence, as_of, "vote record"
        )


@dataclass(frozen=True)
class SummaryPosted:
    """A plain-language summary must be on the website before the hearing."""

    requirement_id: str = S194Requirement.SUMMARY_POSTED.value
    clauses: tuple[ClauseRef, ...] = (JR1B_P4,)

    def awaits(self, facts: BillFacts, tenure: Tenure) -> bool:
        """Owed only for a hearing, which a committee need not hold."""
        return False

    def obligations(self, facts: BillFacts, tenure: Tenure) -> list[Obligation]:
        """Owed for every bill a joint committee schedules for hearing."""
        if _chamber(tenure) != Chamber.JOINT or not tenure.hearing_dates():
            return []
        return [_obligation(self.requirement_id, tenure)]

    def check(
        self, obligation: Obligation, facts: BillFacts, tenure: Tenure, as_of: datetime
    ) -> RequirementResult:
        """Judge the summary's posting against the first hearing."""
        hearing = tenure.hearing_dates()[0]
        due = _start_of(hearing)
        window = _Window(
            due=due,
            late_after=_end_of(hearing) + LATE_TOLERANCE,
            steps=(
                DerivationStep(
                    description="Hearing", value=hearing, role=StepRole.TRIGGER
                ),
                DerivationStep(
                    description="Plain-language summary due on the website before "
                    "the hearing",
                    value=hearing,
                    clause=JR1B_P4,
                    role=StepRole.BASE_DUE,
                ),
            ),
        )
        evidence = facts.document(DocumentKind.SUMMARY, tenure.committee_id)
        return _judge(
            self.requirement_id, obligation, window, evidence, as_of, "summary"
        )

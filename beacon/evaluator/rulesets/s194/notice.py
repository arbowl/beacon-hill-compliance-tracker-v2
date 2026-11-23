"""Hearing notice: JR 1D para 4 (joint), SR 12 para 2 (Senate), HR 17A(e) (House).

Each hearing is judged on the announcement that set its date. The House committee
on Rules is not subject to HR 17A's notice provisions (HR 7B).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from beacon.domain.evaluation import (
    DerivationStep,
    ProvisionalReason,
    RequirementResult,
    RequirementStatus,
    StepRole,
)
from beacon.domain.evidence import EvidenceRef
from beacon.domain.ids import Chamber
from beacon.domain.local_time import local_date
from beacon.domain.observations import ActionType
from beacon.domain.provenance import ClauseRef, RuleSource
from beacon.evaluator.facts import BillFacts, Hearing, Tenure
from beacon.evaluator.rulesets.base import Obligation
from beacon.evaluator.rulesets.s194.requirements import S194Requirement

JR1D_P4 = ClauseRef(RuleSource.JOINT, "1D", 4)
SR12_P2 = ClauseRef(RuleSource.SENATE, "12", 2)
HR17A_P15 = ClauseRef(RuleSource.HOUSE, "17A", 15)
HR7B_P1 = ClauseRef(RuleSource.HOUSE, "7B", 1)
HOUSE_RULES = "H33"
RULES_EFFECTIVE = date(2025, 6, 26)
_REQUIRED_DAYS = {Chamber.JOINT: 10, Chamber.SENATE: 5, Chamber.HOUSE: 3}
_PERIOD = {
    Chamber.JOINT: "10 days",
    Chamber.SENATE: "5 days",
    Chamber.HOUSE: "72 hours",
}
_CLAUSE = {Chamber.JOINT: JR1D_P4, Chamber.SENATE: SR12_P2, Chamber.HOUSE: HR17A_P15}
_CHANGES = {
    ActionType.HEARING_TIME_CHANGED: "Time change",
    ActionType.HEARING_LOCATION_CHANGED: "Location change",
}


@dataclass(frozen=True)
class _Notice:
    label: str
    announced_on: date
    hearing_date: date
    required: int
    period: str
    clause: ClauseRef
    evidence: EvidenceRef

    @property
    def days(self) -> int:
        return (self.hearing_date - self.announced_on).days

    @property
    def exempt(self) -> bool:
        return self.announced_on < RULES_EFFECTIVE

    @property
    def late(self) -> bool:
        return not self.exempt and self.days < self.required

    def describe(self) -> str:
        return (
            f"{self.label} posted {self.announced_on} for the {self.hearing_date} "
            f"hearing: {self.days} days' notice, {self.period} required"
        )


def _chamber(tenure: Tenure) -> Chamber:
    return Chamber.from_prefix(tenure.committee_id[0])


def _judged(hearing: Hearing, chamber: Chamber) -> _Notice | None:
    notice = hearing.notice
    if notice is None or notice.announced_on is None:
        return None
    label = (
        "New hearing date"
        if notice.action_type == ActionType.HEARING_RESCHEDULED
        else "Hearing notice"
    )
    return _Notice(
        label,
        notice.announced_on,
        hearing.hearing_date,
        _REQUIRED_DAYS[chamber],
        _PERIOD[chamber],
        _CLAUSE[chamber],
        notice.evidence,
    )


def _set_aside(hearing: Hearing) -> list[DerivationStep]:
    """One step per announcement of the hearing other than the one judged."""
    steps: list[DerivationStep] = []
    held = hearing.hearing_date
    counted = False
    for n in hearing.notices:
        if n == hearing.notice:
            counted = True
            continue
        if n.announced_on is None:
            continue
        if n.action_type in _CHANGES:
            text = (
                f"{_CHANGES[n.action_type]} posted {n.announced_on} for the {held} "
                "hearing: not judged"
            )
        elif not counted and hearing.notice is not None:
            text = (
                f"Notice posted {n.announced_on} for {n.hearing_date}: superseded "
                f"when the hearing was set for {held}"
            )
        elif n.announced_on >= held:
            text = (
                f"Update posted {n.announced_on}, on or after the {held} hearing: "
                "clerical correction, not judged"
            )
        else:
            text = (
                f"Restatement of the {held} hearing posted {n.announced_on}: not judged"
            )
        steps.append(DerivationStep(description=text, value=n.announced_on))
    return steps


@dataclass(frozen=True)
class HearingNotice:
    """Hearings must be announced, with the required notice, before they are held."""

    requirement_id: str = S194Requirement.HEARING_NOTICE.value
    clauses: tuple[ClauseRef, ...] = (JR1D_P4, SR12_P2, HR17A_P15, HR7B_P1)

    def awaits(self, facts: BillFacts, tenure: Tenure) -> bool:
        """Owed only for a hearing, which a committee need not hold."""
        return False

    def obligations(self, facts: BillFacts, tenure: Tenure) -> list[Obligation]:
        """One obligation per committee tenure with a hearing.

        None for House Rules, which HR 7B exempts from HR 17A's notice provisions.
        """
        if (
            _chamber(tenure) not in _REQUIRED_DAYS
            or tenure.committee_id == HOUSE_RULES
            or not tenure.hearing_dates()
        ):
            return []
        return [
            Obligation(
                obligation_id=f"{tenure.tenure_id}:{self.requirement_id}",
                requirement_id=self.requirement_id,
                tenure_id=tenure.tenure_id,
                triggered_by=(tenure.referral,),
            )
        ]

    def check(
        self, obligation: Obligation, facts: BillFacts, tenure: Tenure, as_of: datetime
    ) -> RequirementResult:
        """Judge each hearing on the announcement that set its date."""
        chamber = _chamber(tenure)
        today = local_date(as_of)
        hearings = tenure.hearings()
        records = [r for h in hearings if (r := _judged(h, chamber)) is not None]
        steps = [s for h in hearings for s in _set_aside(h)]
        if steps:
            steps.append(
                DerivationStep(
                    description=(
                        "Tracker policy: each hearing is judged on the announcement "
                        "that set its date; restatements, corrections, and time or "
                        "place updates are not judged"
                    ),
                    role=StepRole.TOLERANCE,
                )
            )
        steps.append(
            DerivationStep(
                description=(
                    f"Notice rules in effect for announcements from {RULES_EFFECTIVE}; "
                    "earlier announcements are exempt"
                ),
                value=RULES_EFFECTIVE,
                role=StepRole.TOLERANCE,
            )
        )
        for r in records:
            steps.append(
                DerivationStep(
                    description=r.describe() + (" (exempt)" if r.exempt else ""),
                    value=r.announced_on,
                    clause=r.clause,
                    role=StepRole.TRIGGER,
                )
            )
        required = _REQUIRED_DAYS[chamber]
        unannounced = [h.hearing_date for h in hearings if _judged(h, chamber) is None]
        missing = [d for d in unannounced if d >= RULES_EFFECTIVE]
        overdue = [d for d in missing if today >= d - timedelta(days=required)]
        pending = [d for d in missing if d not in overdue]
        for d in missing:
            steps.append(
                DerivationStep(
                    description=f"No announcement found for the {d} hearing",
                    value=d - timedelta(days=required),
                    clause=_CLAUSE[chamber],
                    role=StepRole.BASE_DUE,
                )
            )
        late = sorted((r for r in records if r.late), key=lambda r: r.days - r.required)
        status, reason, resolves_on = RequirementStatus.MET, None, None
        due: date | None = None
        completed_on: date | None = None
        if overdue:
            status = RequirementStatus.UNMET
            due = overdue[0] - timedelta(days=required)
            detail = f"No announcement found for the {overdue[0]} hearing"
        elif late:
            status = RequirementStatus.UNMET
            worst = late[0]
            due = worst.hearing_date - timedelta(days=worst.required)
            completed_on = worst.announced_on
            detail = worst.describe()
        elif pending:
            status, reason = (
                RequirementStatus.PROVISIONAL,
                ProvisionalReason.NOT_YET_DUE,
            )
            due = resolves_on = pending[0] - timedelta(days=required)
            detail = f"No announcement yet for the {pending[0]} hearing; due {due}"
        elif records and all(r.exempt for r in records):
            detail = f"Announced before {RULES_EFFECTIVE}: exempt from notice periods"
        elif records:
            first = min(records, key=lambda r: r.days - r.required)
            completed_on = first.announced_on
            detail = f"Notice given: {first.describe()}"
        else:
            detail = (
                f"Hearings held before {RULES_EFFECTIVE}: exempt from notice periods"
            )
        evidence = [tenure.referral, *(n.evidence for h in hearings for n in h.notices)]
        return RequirementResult(
            requirement_id=self.requirement_id,
            obligation_id=obligation.obligation_id,
            status=status,
            reason=reason,
            detail=detail,
            due=due,
            resolves_on=resolves_on,
            completed_on=completed_on,
            derivation=tuple(steps),
            evidence=tuple(dict.fromkeys(evidence)),
        )

"""Hearing notice: JR 1D paras 4-5 (joint), SR 12 para 2 (Senate), HR 17A(e) (House).

The House committee on Rules is not subject to HR 17A's notice provisions (HR 7B).
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
from beacon.evaluator.facts import BillFacts, Tenure
from beacon.evaluator.rulesets.base import Obligation
from beacon.evaluator.rulesets.s194.requirements import S194Requirement

JR1D_P4 = ClauseRef(RuleSource.JOINT, "1D", 4)
JR1D_P5 = ClauseRef(RuleSource.JOINT, "1D", 5)
SR12_P2 = ClauseRef(RuleSource.SENATE, "12", 2)
HR17A_P15 = ClauseRef(RuleSource.HOUSE, "17A", 15)
HR7B_P1 = ClauseRef(RuleSource.HOUSE, "7B", 1)
HOUSE_RULES = "H33"
RULES_EFFECTIVE = date(2025, 6, 26)
AMENDMENT_DAYS = 3
_INITIAL_DAYS = {Chamber.JOINT: 10, Chamber.SENATE: 5, Chamber.HOUSE: 3}
_INITIAL_CLAUSE = {
    Chamber.JOINT: JR1D_P4,
    Chamber.SENATE: SR12_P2,
    Chamber.HOUSE: HR17A_P15,
}
_CHANGES = (ActionType.HEARING_LOCATION_CHANGED, ActionType.HEARING_TIME_CHANGED)


@dataclass(frozen=True)
class _Notice:
    label: str
    announced_on: date
    hearing_date: date
    required: int
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
        period = (
            "72 hours" if self.required == AMENDMENT_DAYS else f"{self.required} days"
        )
        return (
            f"{self.label} posted {self.announced_on} for the {self.hearing_date} "
            f"hearing: {self.days} days' notice, {period} required"
        )


def _chamber(tenure: Tenure) -> Chamber:
    return Chamber.from_prefix(tenure.committee_id[0])


def _notices(
    tenure: Tenure, chamber: Chamber
) -> tuple[list[_Notice], list[DerivationStep]]:
    """Port of v1's notice walk: judge each announcement against the hearing it set."""
    records: list[_Notice] = []
    notes: list[DerivationStep] = []
    current: date | None = None
    announcements = sorted(
        (n for n in tenure.hearing_notices if n.announced_on is not None),
        key=lambda n: n.announced_on or date.min,
    )
    for n in announcements:
        assert n.announced_on is not None
        if n.action_type in (
            ActionType.HEARING_SCHEDULED,
            ActionType.HEARING_RESCHEDULED,
        ):
            if n.hearing_date is None:
                continue
            current = n.hearing_date
        elif n.action_type not in _CHANGES or current is None:
            continue
        retroactive = n.announced_on >= current
        earlier = {r.hearing_date for r in records}
        duplicate = n.action_type == ActionType.HEARING_SCHEDULED and current in earlier
        if (
            retroactive
            and records
            and (n.action_type != ActionType.HEARING_SCHEDULED or duplicate)
        ):
            notes.append(
                DerivationStep(
                    description=(
                        f"Update posted {n.announced_on}, on or after the {current} "
                        "hearing: clerical correction, not judged"
                    ),
                    value=n.announced_on,
                )
            )
            continue
        new_date = n.action_type == ActionType.HEARING_SCHEDULED or (
            n.action_type == ActionType.HEARING_RESCHEDULED and current not in earlier
        )
        if new_date:
            label = "Hearing notice" if not records else "New hearing date"
            required, clause = _INITIAL_DAYS[chamber], _INITIAL_CLAUSE[chamber]
        elif chamber == Chamber.JOINT:
            label = "Hearing change"
            required, clause = AMENDMENT_DAYS, JR1D_P5
        else:
            notes.append(
                DerivationStep(
                    description=(
                        f"Change posted {n.announced_on}: {chamber.value} rules set "
                        "no notice period for changes to a scheduled hearing"
                    ),
                    value=n.announced_on,
                    clause=_INITIAL_CLAUSE[chamber],
                )
            )
            continue
        records.append(
            _Notice(label, n.announced_on, current, required, clause, n.evidence)
        )
    return records, notes


@dataclass(frozen=True)
class HearingNotice:
    """Hearings must be announced, with the required notice, before they are held."""

    requirement_id: str = S194Requirement.HEARING_NOTICE.value
    clauses: tuple[ClauseRef, ...] = (JR1D_P4, JR1D_P5, SR12_P2, HR17A_P15, HR7B_P1)

    def obligations(self, facts: BillFacts, tenure: Tenure) -> list[Obligation]:
        """One obligation per committee tenure with a hearing.

        None for House Rules, which HR 7B exempts from HR 17A's notice provisions.
        """
        if (
            _chamber(tenure) not in _INITIAL_DAYS
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
        """Judge every announcement, and every hearing that had none."""
        chamber = _chamber(tenure)
        today = local_date(as_of)
        records, steps = _notices(tenure, chamber)
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
        announced = {r.hearing_date for r in records}
        required = _INITIAL_DAYS[chamber]
        unannounced = [d for d in tenure.hearing_dates() if d not in announced]
        missing = [d for d in unannounced if d >= RULES_EFFECTIVE]
        overdue = [d for d in missing if today >= d - timedelta(days=required)]
        pending = [d for d in missing if d not in overdue]
        for d in missing:
            steps.append(
                DerivationStep(
                    description=f"No announcement found for the {d} hearing",
                    value=d - timedelta(days=required),
                    clause=_INITIAL_CLAUSE[chamber],
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
        evidence = [tenure.referral, *(r.evidence for r in records)]
        evidence += [
            n.evidence for n in tenure.hearing_notices if n.announced_on is None
        ]
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

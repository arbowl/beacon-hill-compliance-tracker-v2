"""Joint committee report-out deadlines (Joint Rules 10 and 30, House Rule 27)."""

from dataclasses import dataclass, field
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
from beacon.domain.provenance import ClauseRef, RuleSource
from beacon.evaluator.calendar import AnnualSession, SessionCalendar, Weekday
from beacon.evaluator.facts import BillFacts, Tenure
from beacon.evaluator.rulesets.base import Obligation
from beacon.evaluator.rulesets.s194.requirements import S194Requirement

JR10_P1 = ClauseRef(RuleSource.JOINT, "10", 1)
JR10_P2 = ClauseRef(RuleSource.JOINT, "10", 2)
HR27_P2 = ClauseRef(RuleSource.HOUSE, "27", 2)
HR27_P3 = ClauseRef(RuleSource.HOUSE, "27", 3)
JR30 = ClauseRef(RuleSource.JOINT, "30", 1)
HEALTH_CARE_FINANCING = "J24"
REPORT_WINDOW = timedelta(days=60)
CHAIR_EXTENSION_LIMIT = timedelta(days=90)


@dataclass(frozen=True)
class _Deadline:
    due: date
    steps: tuple[DerivationStep, ...]
    evidence: tuple[EvidenceRef, ...]
    awaiting_hearing: bool = False


@dataclass(frozen=True)
class ReportOutDeadline:
    """Joint committees must make final report by the filing branch's deadline."""

    calendar: SessionCalendar = field(default_factory=lambda: SessionCalendar(194))
    requirement_id: str = S194Requirement.REPORT_OUT_DEADLINE.value
    clauses: tuple[ClauseRef, ...] = (JR10_P1, JR10_P2, HR27_P2, HR27_P3, JR30)

    def awaits(self, facts: BillFacts, tenure: Tenure) -> bool:
        """Every Joint tenure owes a report from referral; nothing waits."""
        return False

    def obligations(self, facts: BillFacts, tenure: Tenure) -> list[Obligation]:
        """One obligation per joint committee tenure."""
        if Chamber.from_prefix(tenure.committee_id[0]) != Chamber.JOINT:
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
        """Compare the tenure's report or discharge against its deadline."""
        today = local_date(as_of)
        if facts.bill.filed_in != Chamber.HOUSE:
            deadline = self._senate_deadline(tenure, facts.bill.filed_in)
        elif (late_hcf := self._late_hcf_deadline(facts, tenure)) is not None:
            deadline = late_hcf
        else:
            deadline = self._house_deadline(tenure, today)
        return self._outcome(obligation, tenure, deadline, today)

    def _late_hcf_deadline(self, facts: BillFacts, tenure: Tenure) -> _Deadline | None:
        if tenure.committee_id != HEALTH_CARE_FINANCING:
            return None
        cal = self.calendar
        cutoff = cal.nth_weekday(3, Weekday.WEDNESDAY, 3, AnnualSession.SECOND)
        if tenure.referred_on <= cutoff.value:
            return None
        prior = next(
            (
                t
                for t in facts.tenures
                if t.referred_on < tenure.referred_on
                and t.committee_id != HEALTH_CARE_FINANCING
                and Chamber.from_prefix(t.committee_id[0]) == Chamber.JOINT
            ),
            None,
        )
        if prior is None:
            return None
        due = cal.last_weekday(Weekday.WEDNESDAY, 5, AnnualSession.SECOND)
        steps = (
            DerivationStep(
                description="Filed in the House: deadline set by House rule",
                clause=JR10_P1,
            ),
            DerivationStep(
                description=(
                    f"Initially referred to joint standing committee "
                    f"{prior.committee_id} on {prior.referred_on}"
                ),
                value=prior.referred_on,
                role=StepRole.TRIGGER,
            ),
            DerivationStep(
                description=(
                    f"Referred to Health Care Financing after the "
                    f"{cutoff.description}: due the {due.description}"
                ),
                value=due.value,
                role=StepRole.BASE_DUE,
                clause=HR27_P2,
            ),
        )
        return _Deadline(due.value, steps, (prior.referral,))

    def _house_deadline(self, tenure: Tenure, today: date) -> _Deadline:
        cal = self.calendar
        december = cal.nth_weekday(3, Weekday.WEDNESDAY, 12, AnnualSession.FIRST)
        cutoff = cal.nth_weekday(3, Weekday.WEDNESDAY, 3, AnnualSession.SECOND)
        steps = [
            DerivationStep(
                description="Filed in the House: deadline set by House rule",
                clause=JR10_P1,
            )
        ]
        held = [d for d in tenure.hearing_dates() if d <= today]
        if not held:
            steps.append(
                DerivationStep(
                    description=(
                        f"No hearing held as of {today}: no final report may be made "
                        f"after the {cutoff.description}"
                    ),
                    value=cutoff.value,
                    role=StepRole.BASE_DUE,
                    clause=HR27_P2,
                )
            )
            return _Deadline(cutoff.value, tuple(steps), (), awaiting_hearing=True)
        heard_on = held[0]
        evidence = [n.evidence for n in tenure.hearing_notices]
        steps.append(
            DerivationStep(description="Heard", value=heard_on, role=StepRole.TRIGGER)
        )
        base = heard_on + REPORT_WINDOW
        if heard_on >= december.value:
            due = max(base, cutoff.value)
            steps.append(
                DerivationStep(
                    description=(
                        f"Heard on or after the {december.description}: 60 calendar "
                        f"days after the hearing ({base}) or the {cutoff.description}, "
                        "whichever is later"
                    ),
                    value=due,
                    role=StepRole.BASE_DUE,
                    clause=HR27_P2,
                )
            )
            steps.extend(
                DerivationStep(
                    description=(
                        f"Extension recorded {e.granted_on} not recognized: no "
                        "extension provision for matters heard after December"
                    ),
                    value=e.until,
                    role=StepRole.EXTENSION_IGNORED,
                    clause=HR27_P2,
                )
                for e in tenure.extensions
            )
            return _Deadline(due, tuple(steps), tuple(evidence))
        due = base
        steps.append(
            DerivationStep(
                description=(
                    f"Heard before the {december.description}: "
                    "60 calendar days after the hearing"
                ),
                value=base,
                role=StepRole.BASE_DUE,
                clause=HR27_P2,
            )
        )
        if tenure.extensions:
            ext = max(
                tenure.extensions, key=lambda e: (e.granted_on, e.evidence.row_index)
            )
            ninety = heard_on + CHAIR_EXTENSION_LIMIT
            until = ext.until or ninety
            if ext.until is None:
                note = "without a date: the chair's additional 30 days assumed"
            elif until <= ninety:
                note = f"to {until}, within the chair's additional 30 calendar days"
            else:
                note = f"to {until}, beyond 90 days by House extension order"
            steps.append(
                DerivationStep(
                    description=f"Extended on {ext.granted_on} {note}",
                    value=until,
                    role=StepRole.EXTENSION,
                    clause=HR27_P2,
                )
            )
            evidence.append(ext.evidence)
            due = max(due, until)
        if due > cutoff.value:
            due = cutoff.value
            steps.append(
                DerivationStep(
                    description=(
                        f"Capped at the {cutoff.description}: no final report or "
                        "extension beyond it"
                    ),
                    value=due,
                    role=StepRole.CAP,
                    clause=HR27_P2,
                )
            )
        return _Deadline(due, tuple(steps), tuple(evidence))

    def _senate_deadline(self, tenure: Tenure, filed_in: Chamber) -> _Deadline:
        cal = self.calendar
        referred = tenure.referred_on
        steps = [
            DerivationStep(
                description=(
                    "Filed in the Senate: no Senate rule sets a joint committee "
                    "deadline, so the Joint Rule 10 default applies"
                ),
                clause=JR10_P2,
            ),
            DerivationStep(
                description="Referred", value=referred, role=StepRole.TRIGGER
            ),
        ]
        if tenure.committee_id == HEALTH_CARE_FINANCING:
            cutoff = cal.nth_weekday(4, Weekday.WEDNESDAY, 12, AnnualSession.FIRST)
            fixed = cal.last_weekday(Weekday.WEDNESDAY, 1, AnnualSession.SECOND)
            on_time_phrase = f"on or before the {cutoff.description}"
            early = referred <= cutoff.value
        else:
            cutoff = cal.fixed(10, 1, AnnualSession.FIRST)
            fixed = cal.nth_weekday(1, Weekday.WEDNESDAY, 12, AnnualSession.FIRST)
            on_time_phrase = f"before {cutoff.description}"
            early = referred < cutoff.value
        if early:
            due = fixed.value
            description = f"Referred {on_time_phrase}: due the {fixed.description}"
        else:
            due = referred + REPORT_WINDOW
            description = "Referred later: due within 60 days of referral"
        steps.append(
            DerivationStep(
                description=description,
                value=due,
                clause=JR10_P2,
                role=StepRole.BASE_DUE,
            )
        )
        evidence = [tenure.referral]
        for e in sorted(
            tenure.extensions, key=lambda e: (e.granted_on, e.evidence.row_index)
        ):
            if e.until is None:
                why = "no new date given"
            elif e.branch.value != filed_in.value:
                why = f"not ordered by the {filed_in.value}, the branch of origination"
            elif e.granted_on > due:
                why = f"granted after the {due} deadline had passed"
            else:
                due = max(due, e.until)
                steps.append(
                    DerivationStep(
                        description=f"Extended on {e.granted_on} to {e.until}",
                        value=e.until,
                        role=StepRole.EXTENSION,
                        clause=JR30,
                    )
                )
                evidence.append(e.evidence)
                continue
            steps.append(
                DerivationStep(
                    description=(
                        f"Extension recorded {e.granted_on} not recognized: {why}"
                    ),
                    value=e.until,
                    role=StepRole.EXTENSION_IGNORED,
                    clause=JR30,
                )
            )
        return _Deadline(due, tuple(steps), tuple(evidence))

    def _outcome(
        self, obligation: Obligation, tenure: Tenure, deadline: _Deadline, today: date
    ) -> RequirementResult:
        due = deadline.due
        steps = list(deadline.steps)
        evidence = [tenure.referral, *deadline.evidence]
        status = RequirementStatus.UNMET
        reason: ProvisionalReason | None = None
        resolves_on: date | None = None
        if (report := tenure.report) is not None:
            evidence.append(report.evidence)
            late = (report.reported_on - due).days
            if late <= 0:
                status = RequirementStatus.MET
                detail = f"Reported on {report.reported_on}, on or before {due}"
            else:
                detail = f"Reported on {report.reported_on}, {late} days after {due}"
        elif tenure.discharged_on is not None:
            if tenure.discharged_on <= due:
                status = RequirementStatus.MET
                detail = (
                    f"Discharged to {tenure.discharged_to} on {tenure.discharged_on}, "
                    f"before {due}"
                )
            else:
                detail = (
                    f"Discharged to {tenure.discharged_to} on {tenure.discharged_on}, "
                    f"after {due} without report"
                )
        elif today <= due:
            status = RequirementStatus.PROVISIONAL
            reason = ProvisionalReason.NOT_YET_DUE
            resolves_on = due
            detail = f"Not yet reported; due {due}"
            if deadline.awaiting_hearing:
                detail += " unless a hearing sets an earlier deadline"
        else:
            detail = f"Not reported by {due}"
            if deadline.awaiting_hearing:
                steps.append(
                    DerivationStep(
                        description=(
                            "Time to report expired: matter should have been reported "
                            "with a recommendation to study"
                        ),
                        clause=HR27_P3,
                    )
                )
        return RequirementResult(
            requirement_id=self.requirement_id,
            obligation_id=obligation.obligation_id,
            status=status,
            reason=reason,
            detail=detail,
            due=due,
            resolves_on=resolves_on,
            completed_on=report.reported_on if report else tenure.discharged_on,
            derivation=tuple(steps),
            evidence=tuple(dict.fromkeys(evidence)),
        )

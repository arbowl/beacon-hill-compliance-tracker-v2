"""Tests for the evaluation engine."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from beacon.domain.evaluation import (
    ComplianceLabel,
    DerivationStep,
    ProvisionalReason,
    RequirementResult,
    RequirementStatus,
    default_roll_up,
)
from beacon.domain.ids import BillKey
from beacon.domain.observations import ActionType as A
from beacon.domain.provenance import ClauseRef, RuleSource
from beacon.evaluator.clauses import ClauseIndex
from beacon.evaluator.engine import evaluate
from beacon.evaluator.facts import BillFacts, Tenure, project
from beacon.evaluator.rulesets.base import Obligation, Ruleset
from beacon.evaluator.rulesets.s194.report_out import ReportOutDeadline
from tests.builders import history, row

ET = ZoneInfo("America/New_York")
RULESET = Ruleset("s194-report-out", "1", (ReportOutDeadline(),), default_roll_up)
RUN = UUID(int=1)
HEARD = [
    row("2025-02-01", A.REFERRED, "J19"),
    row("2025-02-01", A.HEARING_SCHEDULED, "J19", hearing_date="2025-03-04"),
]


def at(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(hour=12, tzinfo=ET)


def facts_for(rows: list[dict], bill_id: str = "H1") -> BillFacts:
    return project(BillKey(194, bill_id), [history(bill_id, rows)])


def run(facts: BillFacts, as_of: str, clause_index: ClauseIndex, ruleset=RULESET):
    return evaluate(facts, "J19", ruleset, clause_index, at(as_of), RUN)


@pytest.mark.parametrize(
    ("rows", "as_of", "label"),
    [
        ([*HEARD, row("2025-04-01", A.REPORTED, "J19")], "2025-06-01", "Compliant"),
        (HEARD, "2025-06-01", "Non-Compliant"),
        (HEARD, "2025-04-01", "Provisional"),
    ],
)
def test_labels(rows, as_of, label, clause_index):
    (evaluation,) = run(facts_for(rows), as_of, clause_index)
    assert evaluation.label == ComplianceLabel(label)
    assert evaluation.ruleset_id == RULESET.ruleset_id


def test_citations_pin_clause_text(clause_index):
    (evaluation,) = run(facts_for(HEARD), "2025-06-01", clause_index)
    citations = {c.ref: c.text_hash for c in evaluation.results[0].clauses}
    hr27 = ClauseRef(RuleSource.HOUSE, "27", 2)
    jr10 = ClauseRef(RuleSource.JOINT, "10", 1)
    assert citations[hr27] == clause_index.get(hr27).text_hash
    assert jr10 in citations


def test_deterministic(clause_index):
    facts = facts_for(HEARD)
    assert run(facts, "2025-06-01", clause_index) == run(
        facts, "2025-06-01", clause_index
    )


def test_fingerprint_tracks_evidence_not_time(clause_index):
    facts = facts_for(HEARD)
    (earlier,) = run(facts, "2025-04-01", clause_index)
    (later,) = run(facts, "2025-06-01", clause_index)
    (changed,) = run(facts_for(HEARD), "2025-06-01", clause_index)
    assert earlier.input_fingerprint == later.input_fingerprint
    assert earlier.label != later.label
    assert changed.input_fingerprint != later.input_fingerprint


def test_missing_tenure_is_provisional_missing_data(clause_index):
    (evaluation,) = run(
        facts_for([row("2025-02-01", A.REFERRED, "J33")]), "2025-06-01", clause_index
    )
    assert evaluation.label == ComplianceLabel.PROVISIONAL
    (result,) = evaluation.results
    assert result.reason == ProvisionalReason.MISSING_DATA
    assert evaluation.tenure_id.endswith(":unobserved")


@dataclass(frozen=True)
class _AlwaysMet:
    requirement_id: str = "always_met"
    clauses: tuple[ClauseRef, ...] = ()
    cite: ClauseRef | None = None

    def obligations(self, facts: BillFacts, tenure: Tenure) -> list[Obligation]:
        return [
            Obligation(
                f"{tenure.tenure_id}:x",
                self.requirement_id,
                tenure.tenure_id,
                (tenure.referral,),
            )
        ]

    def check(self, obligation, facts, tenure, as_of) -> RequirementResult:
        return RequirementResult(
            requirement_id=self.requirement_id,
            obligation_id=obligation.obligation_id,
            status=RequirementStatus.MET,
            detail="ok",
            derivation=(DerivationStep(description="cited", clause=self.cite),),
        )


def _all_met(results: Sequence[RequirementResult]) -> ComplianceLabel:
    return ComplianceLabel.COMPLIANT


def test_swapped_ruleset_reevaluates_same_facts(clause_index):
    facts = facts_for(HEARD)
    alternative = Ruleset("alt", "1", (_AlwaysMet(),), _all_met)
    (original,) = run(facts, "2025-06-01", clause_index)
    (swapped,) = run(facts, "2025-06-01", clause_index, ruleset=alternative)
    assert original.label == ComplianceLabel.NON_COMPLIANT
    assert swapped.label == ComplianceLabel.COMPLIANT
    assert swapped.input_fingerprint != original.input_fingerprint


def test_citing_nonexistent_clause_fails_loudly(clause_index):
    bogus = _AlwaysMet(cite=ClauseRef(RuleSource.HOUSE, "27", 9))
    with pytest.raises(KeyError):
        run(
            facts_for(HEARD),
            "2025-06-01",
            clause_index,
            ruleset=Ruleset("bad", "1", (bogus,), _all_met),
        )


def test_bill_carried_by_vehicle_is_not_judged(clause_index):
    rider = history(
        "S185",
        [
            row("2025-02-27", A.REFERRED, "J47"),
            row(
                "2025-12-18",
                A.ACCOMPANIED,
                text="Accompanied a study order, see S2891",
                related_bills=("S2891",),
            ),
        ],
    )
    vehicle = history(
        "S2891",
        [row("2025-12-18", A.REPORTED, "J47"), row("2025-12-24", A.REFERRED, "J19")],
    )
    facts = project(
        BillKey(194, "S185"),
        [rider],
        vehicle_history=lambda b: [vehicle] if b == "S2891" else [],
    )
    assert run(facts, "2026-06-01", clause_index) == []
    alone = project(BillKey(194, "S185"), [rider])
    (missing,) = run(alone, "2026-06-01", clause_index)
    assert missing.results[0].reason == ProvisionalReason.MISSING_DATA

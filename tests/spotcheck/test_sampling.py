"""Signatures and seeded, label-balanced draws."""

from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4

from beacon.domain.evaluation import (
    ComplianceLabel,
    DerivationStep,
    ProvisionalReason,
    RequirementResult,
    RequirementStatus,
    StepRole,
    TenureEvaluation,
)
from beacon.domain.ids import BillKey
from beacon.spotcheck.sampling import draw, signature


def _evaluation(
    bill_id: str,
    label: ComplianceLabel,
    status: RequirementStatus = RequirementStatus.MET,
    roles: tuple[StepRole, ...] = (),
) -> TenureEvaluation:
    reason = (
        ProvisionalReason.NOT_YET_DUE
        if status == RequirementStatus.PROVISIONAL
        else None
    )
    result = RequirementResult(
        requirement_id="report_out_deadline",
        obligation_id=f"{bill_id}:report_out_deadline",
        status=status,
        reason=reason,
        detail="",
        derivation=tuple(DerivationStep(description="step", role=r) for r in roles),
    )
    return TenureEvaluation(
        evaluation_id=uuid4(),
        run_id=uuid4(),
        bill=BillKey(194, bill_id),
        committee_id="J19",
        tenure_id=f"194/{bill_id}:J19:2025-01-01",
        ruleset_id="s194",
        ruleset_version="6",
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        label=label,
        results=(result,),
        input_fingerprint="f",
    )


def test_signature_names_status_reason_and_notable_steps():
    e = _evaluation(
        "H1",
        ComplianceLabel.PROVISIONAL,
        RequirementStatus.PROVISIONAL,
        (StepRole.TRIGGER, StepRole.CAP, StepRole.EXTENSION),
    )
    assert signature(e) == (
        "Provisional | report_out_deadline=Provisional:not_yet_due[cap+extension]"
    )


def _population() -> list[TenureEvaluation]:
    unmet = [
        _evaluation(f"H{i}", ComplianceLabel.NON_COMPLIANT, RequirementStatus.UNMET)
        for i in range(1, 40)
    ]
    capped = [
        _evaluation(
            f"H{i}",
            ComplianceLabel.NON_COMPLIANT,
            RequirementStatus.UNMET,
            (StepRole.CAP,),
        )
        for i in range(40, 45)
    ]
    met = [_evaluation(f"S{i}", ComplianceLabel.COMPLIANT) for i in range(1, 4)]
    pending = [
        _evaluation(f"S{i}", ComplianceLabel.PROVISIONAL, RequirementStatus.PROVISIONAL)
        for i in range(10, 12)
    ]
    return unmet + capped + met + pending


def test_draw_yields_each_evaluation_once_and_is_seeded():
    population = _population()
    first = [e.evaluation_id for e in draw(population, seed=7)]
    assert sorted(first) == sorted(e.evaluation_id for e in population)
    assert first == [e.evaluation_id for e in draw(reversed(population), seed=7)]
    assert first != [e.evaluation_id for e in draw(population, seed=8)]


def test_draw_rotates_labels_then_signatures():
    head = list(draw(_population(), seed=3))[:6]
    assert Counter(e.label for e in head[:3]) == Counter(ComplianceLabel)
    signatures = {
        signature(e) for e in head if e.label == ComplianceLabel.NON_COMPLIANT
    }
    assert len(signatures) == 2

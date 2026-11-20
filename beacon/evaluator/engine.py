"""Evaluates a bill's facts against a ruleset."""

import hashlib
import json
from datetime import datetime
from uuid import UUID, uuid5

from beacon.domain.evaluation import (
    ClauseCitation,
    ComplianceLabel,
    ProvisionalReason,
    RequirementResult,
    RequirementStatus,
    TenureEvaluation,
)
from beacon.evaluator.clauses import ClauseIndex
from beacon.evaluator.facts import BillFacts
from beacon.evaluator.rulesets.base import Ruleset

TENURE_OBSERVED = "tenure_observed"
_ID_NAMESPACE = UUID("6f1c3b2e-5d4a-4f7e-9a0b-2c8d1e3f4a5b")


def _cite(result: RequirementResult, clauses: ClauseIndex) -> RequirementResult:
    refs = dict.fromkeys(s.clause for s in result.derivation if s.clause is not None)
    citations = tuple(
        ClauseCitation(ref=r, text_hash=clauses.get(r).text_hash) for r in refs
    )
    return result.model_copy(update={"clauses": citations})


def _fingerprint(
    facts: BillFacts, ruleset: Ruleset, results: tuple[RequirementResult, ...]
) -> str:
    material = {
        "observations": sorted(str(o) for o in facts.source_observations),
        "ruleset": [ruleset.ruleset_id, ruleset.version],
        "clauses": sorted({c.text_hash for r in results for c in r.clauses}),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _evaluation(
    facts: BillFacts,
    committee_id: str,
    tenure_id: str,
    ruleset: Ruleset,
    results: tuple[RequirementResult, ...],
    label: ComplianceLabel,
    as_of: datetime,
    run_id: UUID,
) -> TenureEvaluation:
    parts = (run_id, tenure_id, ruleset.ruleset_id, ruleset.version, as_of.isoformat())
    key = "|".join(str(p) for p in parts)
    return TenureEvaluation(
        evaluation_id=uuid5(_ID_NAMESPACE, key),
        run_id=run_id,
        bill=facts.bill,
        committee_id=committee_id,
        tenure_id=tenure_id,
        ruleset_id=ruleset.ruleset_id,
        ruleset_version=ruleset.version,
        as_of=as_of,
        label=label,
        results=results,
        input_fingerprint=_fingerprint(facts, ruleset, results),
    )


def evaluate(
    facts: BillFacts,
    committee_id: str,
    ruleset: Ruleset,
    clauses: ClauseIndex,
    as_of: datetime,
    run_id: UUID,
) -> list[TenureEvaluation]:
    """Evaluate every tenure of a bill before one committee.

    Deterministic for identical inputs. A committee-originated document with no
    tenure yields nothing. Any other bill with no observed tenure before the
    committee yields one Provisional evaluation with reason MISSING_DATA.
    """
    tenures = facts.tenures_for(committee_id)
    if not tenures and facts.origin is not None:
        return []
    if not tenures:
        tenure_id = f"{facts.bill}:{committee_id}:unobserved"
        missing = RequirementResult(
            requirement_id=TENURE_OBSERVED,
            obligation_id=f"{tenure_id}:{TENURE_OBSERVED}",
            status=RequirementStatus.PROVISIONAL,
            reason=ProvisionalReason.MISSING_DATA,
            detail=f"No referral to {committee_id} found in observed bill history",
        )
        return [
            _evaluation(
                facts,
                committee_id,
                tenure_id,
                ruleset,
                (missing,),
                ComplianceLabel.PROVISIONAL,
                as_of,
                run_id,
            )
        ]
    evaluations = []
    for tenure in tenures:
        results = tuple(
            _cite(requirement.check(obligation, facts, tenure, as_of), clauses)
            for requirement in ruleset.requirements
            for obligation in requirement.obligations(facts, tenure)
        )
        if not results:
            continue
        evaluations.append(
            _evaluation(
                facts,
                committee_id,
                tenure.tenure_id,
                ruleset,
                results,
                ruleset.roll_up(results),
                as_of,
                run_id,
            )
        )
    return evaluations

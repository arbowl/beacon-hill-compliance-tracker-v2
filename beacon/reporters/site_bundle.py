"""Builds the site's v1 push bundle for a committee and writes it gzipped."""

import gzip
from pathlib import Path

from beacon_contract import v1

from beacon.domain.evaluation import RequirementResult, TenureEvaluation
from beacon.domain.evidence import EvidenceRef
from beacon.domain.ids import CommitteeKey
from beacon.domain.provenance import ClauseRef
from beacon.evaluator.clauses import ClauseIndex
from beacon.reporters.base import EvidenceItem, ReportInput

BUNDLE_SUFFIX = ".bundle.json.gz"


class BundleError(ValueError):
    """The report cannot be expressed as a valid bundle."""


def _ref(ref: ClauseRef) -> v1.ClauseRef:
    return v1.ClauseRef(source=ref.source.value, rule=ref.rule, paragraph=ref.paragraph)


def _evidence(ref: EvidenceRef, item: EvidenceItem | None) -> v1.Evidence:
    line = None
    if item is not None:
        line = v1.EvidenceLine(
            source=item.source.value,
            occurred_on=item.occurred_on,
            text=item.text,
            url=item.url,
        )
    return v1.Evidence(
        observation_id=ref.observation_id, row_index=ref.row_index, line=line
    )


def _requirement(
    result: RequirementResult, evidence: dict[EvidenceRef, EvidenceItem]
) -> v1.RequirementResult:
    return v1.RequirementResult(
        requirement_id=result.requirement_id,
        obligation_id=result.obligation_id,
        status=result.status.value,
        reason=result.reason.value if result.reason else None,
        detail=result.detail,
        due=result.due,
        resolves_on=result.resolves_on,
        completed_on=result.completed_on,
        derivation=tuple(
            v1.DerivationStep(
                description=s.description,
                value=s.value,
                clause=_ref(s.clause) if s.clause else None,
                role=s.role.value if s.role else None,
            )
            for s in result.derivation
        ),
        evidence=tuple(_evidence(ref, evidence.get(ref)) for ref in result.evidence),
        clauses=tuple(
            v1.ClauseCitation(ref=_ref(c.ref), text_hash=c.text_hash)
            for c in result.clauses
        ),
    )


def _tenure(
    evaluation: TenureEvaluation, evidence: dict[EvidenceRef, EvidenceItem]
) -> v1.Tenure:
    referral = evaluation.tenure_id.rsplit(":", 1)[1]
    return v1.Tenure(
        tenure_id=evaluation.tenure_id,
        bill_id=evaluation.bill.bill_id,
        committee_id=evaluation.committee_id,
        referred_on=None if referral == v1.UNOBSERVED else referral,
        label=evaluation.label.value,
        input_fingerprint=evaluation.input_fingerprint,
        requirements=tuple(_requirement(r, evidence) for r in evaluation.results),
    )


def _clauses(
    evaluations: list[TenureEvaluation], index: ClauseIndex
) -> tuple[v1.Clause, ...]:
    """The verbatim text of every cited clause, as it was when evaluated."""
    cited = {
        c.text_hash: c.ref for e in evaluations for r in e.results for c in r.clauses
    }
    clauses = []
    for text_hash, ref in sorted(cited.items()):
        clause = index.get(ref)
        if clause.text_hash != text_hash:
            raise BundleError(f"Text of {ref} changed since evaluation; evaluate again")
        clauses.append(v1.Clause(ref=_ref(ref), text=clause.text, text_hash=text_hash))
    return tuple(clauses)


def build_bundle(report: ReportInput, index: ClauseIndex) -> v1.CommitteeBundle:
    """The committee's evaluations as one validated bundle."""
    evaluations = list(report.evaluations)
    runs = {(e.run_id, e.ruleset_id, e.ruleset_version, e.as_of) for e in evaluations}
    if len(runs) != 1:
        raise BundleError(f"{report.committee_id}: evaluations span {len(runs)} runs")
    if report.committee_name is None or report.committee_url is None:
        raise BundleError(f"{report.committee_id}: committee not in inventory")
    ((run_id, ruleset_id, ruleset_version, as_of),) = runs
    evidence = dict(report.evidence)
    evaluated = {e.bill.bill_id for e in evaluations}
    return v1.CommitteeBundle(
        schema_version=v1.SCHEMA_VERSION,
        session=report.session,
        evaluation_run_id=run_id,
        ruleset_id=ruleset_id,
        ruleset_version=ruleset_version,
        as_of=as_of,
        committee=v1.Committee(
            committee_id=report.committee_id,
            name=report.committee_name,
            chamber=CommitteeKey(report.session, report.committee_id).chamber.value,
            url=report.committee_url,
        ),
        bills=tuple(
            v1.Bill(bill_id=b.bill_id, title=b.title, url=b.url)
            for b in sorted(report.bills, key=lambda b: b.bill_id)
            if b.bill_id in evaluated
        ),
        tenures=tuple(_tenure(e, evidence) for e in evaluations),
        clauses=_clauses(evaluations, index),
        committee_output=tuple(
            v1.CommitteeOutput(
                bill_id=o.bill_id,
                title=o.title,
                originated_on=o.originated_on,
                source_bills=o.source_bills,
            )
            for o in report.committee_output
        ),
    )


class SiteBundleReporter:
    """Writes <as_of date>/<committee>.bundle.json.gz for `beacon push`."""

    def __init__(self, clauses: ClauseIndex) -> None:
        self._clauses = clauses

    def write(self, report: ReportInput, out_dir: Path) -> list[Path]:
        if not report.evaluations:
            return []
        bundle = build_bundle(report, self._clauses)
        path = (
            out_dir
            / bundle.as_of.date().isoformat()
            / f"{report.committee_id}{BUNDLE_SUFFIX}"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            gzip.compress(bundle.model_dump_json().encode("utf-8"), mtime=0)
        )
        return [path]

"""Read-only views over the store, shaped for the browser."""

from collections import Counter
from typing import Any

from beacon.adapters.sqlite_store import SqliteStore
from beacon.config import Settings
from beacon.domain.evaluation import TenureEvaluation
from beacon.domain.ids import BillKey
from beacon.domain.observations import ObservationKind
from beacon.domain.provenance import ClauseRef
from beacon.evaluator.clauses import ClauseIndex
from beacon.evaluator.facts import project
from beacon.orchestrator.runner import ALL_COMMITTEES, PLANS
from beacon.ports.store import Stage
from beacon.scraper.history.committees import COMMITTEE_REGISTRY


def options(settings: Settings, store: SqliteStore) -> dict[str, Any]:
    """Every run option the runner accepts, with defaults."""
    names = {
        c.id: c.canonical_name
        for c in COMMITTEE_REGISTRY.values()
        if c.active_until is None
    }
    names |= {
        c.committee_id: c.name for c in store.inventory.committees(settings.session)
    }
    known = names.items()
    committees = [
        {
            "id": committee_id,
            "name": name,
            "has_data": bool(store.inventory.bills_for(settings.session, committee_id)),
        }
        for committee_id, name in sorted(known)
    ]
    return {
        "session": settings.session,
        "ruleset": settings.ruleset,
        "data_dir": str(settings.data_dir),
        "plans": {name: [s.value for s in stages] for name, stages in PLANS.items()},
        "stages": [s.value for s in Stage],
        "committees": committees,
        "defaults": {"committees": list(settings.committees)},
        "all_committees": ALL_COMMITTEES,
        "flags": {
            "as_of": "See how things stood on an earlier date",
            "full": (
                "Look again for every summary and vote, not only for bills that changed"
            ),
        },
    }


def overview(store: SqliteStore, settings: Settings) -> list[dict[str, Any]]:
    """Each checked committee with its latest label counts."""
    cards = []
    for committee in store.inventory.committees(settings.session):
        evaluations = _latest_evaluations(store, settings, committee.committee_id)
        if not evaluations:
            continue
        cards.append(
            {
                "committee_id": committee.committee_id,
                "name": committee.name,
                "as_of": evaluations[0].as_of.isoformat(),
                "summary": dict(Counter(e.label.value for e in evaluations)),
                "bills": len(evaluations),
                "audit_url": _latest_audit(settings, committee.committee_id),
            }
        )
    return cards


def _latest_audit(settings: Settings, committee_id: str) -> str | None:
    """Browser path of the newest audit report for a committee, if any."""
    reports = sorted(settings.report_dir.glob(f"*/{committee_id}_audit.html"))
    if not reports:
        return None
    return f"/reports/{reports[-1].parent.name}/{reports[-1].name}"


def _latest_evaluations(
    store: SqliteStore, settings: Settings, committee_id: str
) -> list[TenureEvaluation]:
    run_id = store.evaluations.latest_run(
        settings.session, committee_id, settings.ruleset
    )
    return store.evaluations.for_run(run_id) if run_id else []


def committee_results(
    store: SqliteStore, settings: Settings, committee_id: str
) -> dict[str, Any]:
    """One row per evaluated bill, with each requirement's status."""
    evaluations = _latest_evaluations(store, settings, committee_id)
    bills = {
        b.bill_id: b for b in store.inventory.bills_for(settings.session, committee_id)
    }
    committee = store.inventory.committee(settings.session, committee_id)
    totals: dict[str, Counter[str]] = {}
    for e in evaluations:
        for r in e.results:
            totals.setdefault(r.requirement_id, Counter())[r.status.value] += 1
    rows = [
        {
            "bill_id": e.bill.bill_id,
            "title": bills[e.bill.bill_id].title if e.bill.bill_id in bills else None,
            "url": bills[e.bill.bill_id].url if e.bill.bill_id in bills else None,
            "tenure_id": e.tenure_id,
            "label": e.label.value,
            "requirements": {
                r.requirement_id: {
                    "status": r.status.value,
                    "reason": r.reason.value if r.reason else None,
                    "detail": r.detail,
                }
                for r in e.results
            },
        }
        for e in evaluations
    ]
    return {
        "committee_id": committee_id,
        "committee_name": committee.name if committee else committee_id,
        "requirement_totals": {k: dict(v) for k, v in totals.items()},
        "as_of": evaluations[0].as_of.isoformat() if evaluations else None,
        "evaluation_run": str(evaluations[0].run_id) if evaluations else None,
        "summary": dict(Counter(e.label.value for e in evaluations)),
        "bills": rows,
    }


def _clause_text(clauses: ClauseIndex, ref: ClauseRef) -> str | None:
    try:
        return clauses.get(ref).text
    except KeyError:
        return None


def bill_detail(
    store: SqliteStore,
    settings: Settings,
    clauses: ClauseIndex,
    committee_id: str,
    bill_id: str,
) -> dict[str, Any]:
    """Facts, evaluation with clause text, and the observations behind them."""
    bill = BillKey(settings.session, bill_id)
    observations = store.observations.for_bill(settings.session, bill.bill_id)
    hearings = [
        o
        for o in store.observations.of_kind(ObservationKind.HEARING)
        if o.committee_id == committee_id
    ]
    facts = project(bill, observations, hearings)
    evaluations = [
        e
        for e in _latest_evaluations(store, settings, committee_id)
        if e.bill.bill_id == bill.bill_id
    ]
    tenures = [
        {
            "tenure_id": t.tenure_id,
            "committee_id": t.committee_id,
            "referred_on": t.referred_on.isoformat(),
            "hearing_dates": [d.isoformat() for d in t.hearing_dates()],
            "hearing_notices": [
                {
                    "announced_on": n.announced_on.isoformat()
                    if n.announced_on
                    else None,
                    "action_type": n.action_type.value,
                    "hearing_date": n.hearing_date.isoformat()
                    if n.hearing_date
                    else None,
                    "from_docket": n.announced_on is None,
                }
                for n in t.hearing_notices
            ],
            "extensions": [
                {
                    "granted_on": e.granted_on.isoformat(),
                    "until": e.until.isoformat() if e.until else None,
                }
                for e in t.extensions
            ],
            "report": (
                {
                    "reported_on": t.report.reported_on.isoformat(),
                    "text": t.report.raw_text,
                }
                if t.report
                else None
            ),
            "discharged_on": t.discharged_on.isoformat() if t.discharged_on else None,
            "discharged_to": t.discharged_to,
        }
        for t in facts.tenures
    ]
    evaluation_views = []
    for e in evaluations:
        data = e.model_dump(mode="json")
        for result, raw in zip(e.results, data["results"], strict=True):
            for step, raw_step in zip(
                result.derivation, raw["derivation"], strict=True
            ):
                raw_step["clause_text"] = (
                    _clause_text(clauses, step.clause) if step.clause else None
                )
        evaluation_views.append(data)
    record = next(
        (
            b
            for b in store.inventory.bills_for(settings.session, committee_id)
            if b.bill_id == bill.bill_id
        ),
        None,
    )
    return {
        "bill_id": bill.bill_id,
        "title": record.title if record else None,
        "url": record.url if record else None,
        "committee_id": committee_id,
        "tenures": tenures,
        "origin": (
            {
                "originated_on": facts.origin.originated_on.isoformat(),
                "source_bills": list(facts.origin.source_bills),
            }
            if facts.origin
            else None
        ),
        "documents": [
            {
                "kind": d.document_kind.value,
                "committee_id": d.committee_id,
                "first_seen_at": d.first_seen_at.isoformat()
                if d.first_seen_at
                else None,
                "absent_before": d.absent_before.isoformat()
                if d.absent_before
                else None,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "public_from": d.public_from.isoformat() if d.public_from else None,
                "public_basis": d.public_basis.value if d.public_basis else None,
                "vote_date": d.vote_date.isoformat() if d.vote_date else None,
                "url": d.url,
            }
            for d in facts.documents
        ],
        "evaluations": evaluation_views,
        "observations": [
            {
                "observation_id": str(o.observation_id),
                "kind": o.kind.value,
                "extractor": f"{o.extractor_id} v{o.extractor_version}",
                "observed_at": o.observed_at.isoformat(),
                "capture_id": str(o.capture_id),
            }
            for o in sorted(observations, key=lambda o: o.observed_at, reverse=True)
        ],
    }

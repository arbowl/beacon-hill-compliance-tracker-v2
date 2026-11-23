"""Markdown for a spot check: blind source dossiers, answer keys, and rule text."""

from collections.abc import Iterable
from datetime import date, datetime

from beacon.domain.evaluation import RequirementResult, TenureEvaluation
from beacon.domain.evidence import EvidenceRef
from beacon.domain.ids import BillKey
from beacon.domain.local_time import local_date
from beacon.domain.observations import (
    BillHistoryPayload,
    DocumentPayload,
    DocumentSearchPayload,
    HearingPayload,
    Observation,
)
from beacon.domain.provenance import ClauseRef
from beacon.evaluator.clauses import ClauseIndex
from beacon.evaluator.facts import Tenure
from beacon.evaluator.rulesets.base import Ruleset
from beacon.spotcheck.replay import Case


def _cell(text: object) -> str:
    return "" if text is None else str(text).replace("|", "\\|")


def _day(moment: datetime) -> str:
    return local_date(moment).isoformat()


def _count(n: int, noun: str) -> str:
    suffix = "" if n == 1 else ("es" if noun.endswith("h") else "s")
    return f"{n} {noun}{suffix}"


def _header(item: int, case: Case, title: str) -> list[str]:
    stored, bill = case.stored, case.stored.bill
    lines = [f"# Item {item} {title}: {bill.bill_id} before {stored.committee_id}", ""]
    record = case.bill
    filed = BillKey(bill.session, bill.bill_id).filed_in.value
    name = f' "{record.title}"' if record and record.title else ""
    link = f" <{record.url}>" if record else ""
    lines.append(f"- Bill: {bill.bill_id}, filed in the {filed}{name}{link}")
    if case.committee is not None:
        c = case.committee
        lines.append(f"- Committee: {c.committee_id} {c.name} ({c.chamber.value})")
    else:
        lines.append(f"- Committee: {stored.committee_id}")
    if case.tenure is not None:
        lines.append(f"- Tenure under audit: referral dated {case.tenure.referred_on}")
    else:
        lines.append(
            f"- Tenure under audit: none; the pipeline found no referral to "
            f"{stored.committee_id} in the history"
        )
    lines.append(f"- Evaluated as of: {_day(case.evaluation.as_of)} (State House date)")
    return lines


def _history_source(case: Case) -> list[str]:
    lines = ["", "## Bill history as captured", ""]
    if case.history is None:
        return [*lines, "No bill history was captured."]
    lines.append(
        f"Page read {_day(case.history.observed_at)} from <{case.history_url}>."
    )
    if not case.raw_rows:
        return [*lines, "", "The captured page has no table rows."]
    lines += ["", "| # | Cells |", "|---|---|"]
    for r in case.raw_rows:
        cells = " / ".join(_cell(c) for c in r.cells)
        lines.append(f"| r{r.position} | {cells} |")
    return lines


def _hearings(case: Case) -> list[str]:
    lines = [
        "",
        "## Hearings whose docket lists this bill (extracted from hearing pages)",
        "",
    ]
    if not case.hearings:
        return [*lines, "None."]
    bill_id = case.stored.bill.bill_id
    for o in case.hearings:
        payload = o.payload
        if not isinstance(payload, HearingPayload):
            continue
        held = (
            payload.hearing_date.isoformat()
            if payload.hearing_date
            else "date not listed"
        )
        lines.append(
            f"- Hearing {payload.hearing_id}, held {held}; {len(payload.docket)} "
            f"bills on the docket; page read {_day(o.observed_at)}"
        )
        for doc in payload.documents:
            if bill_id in doc.bill_ids:
                created = (
                    f", file created {_day(doc.created_at)}" if doc.created_at else ""
                )
                lines.append(f'  - Attached: "{doc.title}"{created} <{doc.url}>')
    return lines


def _search_outcome(payload: DocumentSearchPayload) -> str:
    if payload.candidates:
        places = "; ".join(dict.fromkeys(c.location for c in payload.candidates))
        return f"found ({places})"
    scope = "every finder ran" if payload.exhaustive else "search incomplete"
    return f"not found ({scope}; tried {', '.join(payload.finders_tried)})"


def _searches(case: Case) -> list[str]:
    committee_id = case.stored.committee_id
    lines = ["", f"## Document searches for {committee_id} (extracted)", ""]
    if not case.searches:
        return [*lines, "No searches recorded."]
    by_kind: dict[str, list[tuple[date, str]]] = {}
    for o in case.searches:
        if isinstance(o.payload, DocumentSearchPayload):
            by_kind.setdefault(o.payload.document_kind.value, []).append(
                (local_date(o.observed_at), _search_outcome(o.payload))
            )
    for kind, searches in sorted(by_kind.items()):
        lines.append(f"- {kind}:")
        for outcome, days in _runs(searches):
            span = (
                days[0].isoformat()
                if days[0] == days[-1]
                else f"{days[0]} to {days[-1]}"
            )
            lines.append(f"  - {span} ({_count(len(days), 'search')}): {outcome}")
    return lines


def _runs(searches: list[tuple[date, str]]) -> list[tuple[str, list[date]]]:
    """Consecutive searches with the same outcome, collapsed."""
    runs: list[tuple[str, list[date]]] = []
    for day, outcome in searches:
        if runs and runs[-1][0] == outcome:
            runs[-1][1].append(day)
        else:
            runs.append((outcome, [day]))
    return runs


def _documents(case: Case) -> list[str]:
    lines = ["", f"## Documents read for {case.stored.committee_id} (extracted)", ""]
    reads: dict[tuple[str, str], list[tuple[Observation, DocumentPayload]]] = {}
    for o in case.documents:
        if isinstance(o.payload, DocumentPayload):
            key = (o.payload.document_kind.value, o.payload.url)
            reads.setdefault(key, []).append((o, o.payload))
    if not reads:
        return [*lines, "None."]
    for (kind, url), history in sorted(reads.items()):
        o, p = history[-1]
        facts = []
        if p.vote_date:
            facts.append(f"vote dated {p.vote_date}")
        if p.motion:
            facts.append(f'motion "{p.motion}"')
        if p.tallies:
            facts.append(
                "tallies " + ", ".join(f"{k} {v}" for k, v in p.tallies.items())
            )
        if p.created_at:
            facts.append(f"file created {_day(p.created_at)}")
        if p.public_from:
            basis = p.public_basis.value if p.public_basis else "unknown basis"
            facts.append(f"public from {_day(p.public_from)} ({basis})")
        facts.append(f"{p.text_length} characters of text")
        first = _day(history[0][0].observed_at)
        last = _day(o.observed_at)
        span = first if first == last else f"{first} to {last}"
        seen = f"read {_count(len(history), 'time')}, {span}"
        lines.append(f"- {kind} <{url}>: {'; '.join(facts)} ({seen})")
    return lines


def render_dossier(item: int, case: Case) -> str:
    """The source material for one tenure, without the pipeline's conclusions."""
    lines = _header(item, case, "dossier")
    lines.append("- Rule text: ../rules.md")
    lines += _history_source(case)
    lines += _hearings(case)
    lines += _searches(case)
    lines += _documents(case)
    return "\n".join(lines) + "\n"


def _classified(case: Case) -> list[str]:
    history = case.history
    if history is None or not isinstance(history.payload, BillHistoryPayload):
        return []
    extractor = f"{history.extractor_id} v{history.extractor_version}"
    lines = [
        "",
        f"## History as classified ({extractor})",
        "",
        "| idx | Date | Branch | Type | Committee | Hearing | New deadline | Related "
        "| Text |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in history.payload.rows:
        cells = (
            r.row_index,
            r.action_date,
            r.branch.value,
            r.action_type.value,
            r.committee_id,
            r.hearing_date,
            r.new_deadline,
            ", ".join(r.related_bills),
            r.raw_text,
        )
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return lines


def _ref(ref: EvidenceRef, case: Case) -> str:
    if case.history is not None and ref.observation_id == case.history.observation_id:
        return (
            f"history idx {ref.row_index}"
            if ref.row_index is not None
            else "bill history"
        )
    for o in (*case.hearings, *case.searches, *case.documents):
        if o.observation_id == ref.observation_id:
            return f"{o.kind.value} read {_day(o.observed_at)}"
    return f"observation {str(ref.observation_id)[:8]}"


def _tenure(tenure: Tenure | None, case: Case) -> list[str]:
    lines = ["", "## Tenure as projected", ""]
    if tenure is None:
        return [*lines, "No tenure: the committee never appears as a referral."]
    lines.append(f"- Referred {tenure.referred_on} ({_ref(tenure.referral, case)})")
    for n in tenure.hearing_notices:
        lines.append(
            f"- Notice: {n.action_type.value}, announced "
            f"{n.announced_on or 'unknown'}, hearing {n.hearing_date or 'unknown'} "
            f"({_ref(n.evidence, case)})"
        )
    for e in tenure.extensions:
        lines.append(
            f"- Extension granted {e.granted_on} until {e.until or 'unspecified'} "
            f"({_ref(e.evidence, case)})"
        )
    if tenure.report is not None:
        r = tenure.report
        lines.append(
            f"- Report: {r.action_type.value} on {r.reported_on} "
            f"({_ref(r.evidence, case)})"
        )
    if tenure.discharged_on is not None:
        to = tenure.discharged_to or "unknown"
        lines.append(f"- Discharged {tenure.discharged_on} to {to}")
    return lines


def _result(result: RequirementResult, case: Case) -> list[str]:
    status = result.status.value + (
        f" ({result.reason.value})" if result.reason else ""
    )
    lines = ["", f"### {result.requirement_id}: {status}", "", result.detail, ""]
    dates = [
        f"{name} {value}"
        for name, value in (
            ("due", result.due),
            ("resolves on", result.resolves_on),
            ("completed", result.completed_on),
        )
        if value is not None
    ]
    if dates:
        lines += ["Dates: " + "; ".join(dates), ""]
    for i, step in enumerate(result.derivation, 1):
        role = f"[{step.role.value}] " if step.role else ""
        value = f" = {step.value}" if step.value else ""
        clause = f" ({step.clause})" if step.clause else ""
        lines.append(f"{i}. {role}{step.description}{value}{clause}")
    if result.evidence:
        refs = dict.fromkeys(_ref(r, case) for r in result.evidence)
        lines += ["", "Evidence: " + "; ".join(refs)]
    return lines


def _statuses(evaluation: TenureEvaluation) -> dict[str, str]:
    return {
        r.requirement_id: r.status.value + (f":{r.reason.value}" if r.reason else "")
        for r in evaluation.results
    }


def _stored(case: Case) -> list[str]:
    stored, now = case.stored, case.evaluation
    lines = [
        "",
        "## Stored evaluation",
        "",
        f"Run {stored.run_id} as of {_day(stored.as_of)} under {stored.ruleset_id} "
        f"v{stored.ruleset_version}: {stored.label.value}.",
    ]
    before, after = _statuses(stored), _statuses(now)
    changed = [
        f"- {key}: stored {before.get(key, 'absent')}, now {after.get(key, 'absent')}"
        for key in sorted(before.keys() | after.keys())
        if before.get(key) != after.get(key)
    ]
    if stored.label != now.label:
        changed.insert(
            0, f"- label: stored {stored.label.value}, now {now.label.value}"
        )
    return [*lines, "", *(changed or ["Same outcome as the current code."])]


def render_key(item: int, case: Case) -> str:
    """The pipeline's interpretation of one tenure, to compare after a blind read."""
    evaluation = case.evaluation
    lines = _header(item, case, "key")
    lines += _classified(case)
    lines += _tenure(case.tenure, case)
    ruleset = f"{evaluation.ruleset_id} v{evaluation.ruleset_version}"
    lines += [
        "",
        f"## Evaluation by current code ({ruleset}): {evaluation.label.value}",
    ]
    for result in evaluation.results:
        lines += _result(result, case)
    lines += _stored(case)
    return "\n".join(lines) + "\n"


def render_rules(
    ruleset: Ruleset, clauses: ClauseIndex, cited: Iterable[ClauseRef]
) -> str:
    """Verbatim text of each requirement's clauses, then any other clause cited."""
    lines = [f"# Rule text for {ruleset.ruleset_id} v{ruleset.version}"]
    shown: set[ClauseRef] = set()
    for requirement in ruleset.requirements:
        lines += ["", f"## {requirement.requirement_id}"]
        for ref in requirement.clauses:
            lines += ["", f"### {ref}", "", clauses.get(ref).text]
            shown.add(ref)
    others = sorted(set(cited) - shown, key=str)
    if others:
        lines += ["", "## Also cited"]
        for ref in others:
            lines += ["", f"### {ref}", "", clauses.get(ref).text]
    return "\n".join(lines) + "\n"

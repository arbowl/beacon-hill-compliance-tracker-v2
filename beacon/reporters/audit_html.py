"""Standalone HTML audit report: each bill's status, why, and the rules cited."""

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from html import escape
from importlib.resources import files
from pathlib import Path

from beacon.domain.evaluation import (
    ClauseCitation,
    ComplianceLabel,
    DerivationStep,
    NoRequirements,
    NoRequirementsReason,
    ProvisionalReason,
    RequirementResult,
    RequirementStatus,
    TenureEvaluation,
)
from beacon.domain.evidence import EvidenceRef
from beacon.domain.inventory import BillRecord
from beacon.evaluator.clauses import ClauseIndex
from beacon.reporters.base import EvidenceItem, ReportInput, run_identity

REQUIREMENT_NAMES = {
    "report_out_deadline": "Reported out by the deadline",
    "hearing_notice": "Hearing announced with enough notice",
    "summary_posted": "Plain-language summary posted",
    "votes_posted": "Committee votes posted",
    "tenure_observed": "Bill record found",
}
LABEL_TEXT = {
    ComplianceLabel.COMPLIANT: "Compliant",
    ComplianceLabel.NON_COMPLIANT: "Non-Compliant",
    ComplianceLabel.PROVISIONAL: "Provisional",
}
REASON_TEXT = {
    ProvisionalReason.NOT_YET_DUE: "the deadline has not passed",
    ProvisionalReason.MISSING_DATA: "information is missing",
    ProvisionalReason.AMBIGUOUS_TIMING: "the timing cannot be confirmed",
    ProvisionalReason.AWAITING_REVIEW: "a document is awaiting review",
}
NO_REQUIREMENTS_TEXT = {
    NoRequirementsReason.NONE_APPLY: "No tracked rule applies to this referral.",
    NoRequirementsReason.NONE_YET: "Nothing is owed yet.",
    NoRequirementsReason.ACCOMPANIED: "It came before the committee only as part of",
    NoRequirementsReason.RECORD_INCOMPLETE: (
        "The legislative record does not show how this bill came before the "
        "committee. It is not judged."
    ),
}
_STATIC = files("beacon.reporters.static")
_LABEL_ORDER = {
    ComplianceLabel.NON_COMPLIANT: 0,
    ComplianceLabel.PROVISIONAL: 1,
    ComplianceLabel.COMPLIANT: 2,
}


def _long(day: date | None) -> str:
    return f"{day:%B} {day.day}, {day.year}" if day else ""


_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _plain(text: str) -> str:
    """Escaped text with ISO dates written out."""
    return escape(_ISO_DATE.sub(lambda m: _long(date.fromisoformat(m.group(0))), text))


def _evidence(items: list[EvidenceItem], folded: bool) -> str:
    if not items:
        return ""
    lines = "".join(
        "<li>"
        + (
            f'<span class="when">{escape(_long(i.occurred_on))}</span> '
            if i.occurred_on
            else ""
        )
        + f'<span class="src">{escape(i.source.value)}.</span> '
        + f"<q>{_plain(i.text)}</q>"
        + (
            f' <a href="{escape(i.url)}" title="{escape(i.url)}" rel="noopener">'
            "source</a>"
            if i.url
            else ""
        )
        + "</li>"
        for i in items
    )
    listing = f'<ul class="evidence">{lines}</ul>'
    if folded:
        return f"<details><summary>Evidence ({len(items)})</summary>{listing}</details>"
    return f'<div class="evidence-head">Evidence</div>{listing}'


def _bill_label(bill_id: str) -> str:
    return f"{bill_id[0]}.{bill_id[1:]}"


@dataclass(frozen=True)
class AuditReporter:
    """Writes <committee>_audit.html: per-bill verdicts, reasons, and rule text."""

    clauses: ClauseIndex

    def _citation(self, citation: ClauseCitation) -> str:
        try:
            clause = self.clauses.get(citation.ref)
        except KeyError:
            return (
                f'<p class="warn">{escape(str(citation.ref))}: rule text not found</p>'
            )
        changed = (
            ""
            if clause.text_hash == citation.text_hash
            else '<p class="warn">The rule text has changed since this was '
            "evaluated.</p>"
        )
        return (
            f'<div class="cite">{escape(str(citation.ref))}</div>'
            f"<blockquote>{escape(clause.text)}</blockquote>{changed}"
        )

    def _steps(self, steps: tuple[DerivationStep, ...]) -> str:
        items = "".join(
            "<li>"
            + _plain(s.description)
            + (f" ({escape(_long(s.value))})" if s.value else "")
            + (
                f' <span class="cite">[{escape(str(s.clause))}]</span>'
                if s.clause
                else ""
            )
            + "</li>"
            for s in steps
        )
        return (
            "<details><summary>How this was worked out</summary>"
            f'<ol class="steps">{items}</ol></details>'
        )

    def _requirement(
        self, result: RequirementResult, evidence: Mapping[EvidenceRef, EvidenceItem]
    ) -> str:
        name = escape(
            REQUIREMENT_NAMES.get(result.requirement_id, result.requirement_id)
        )
        detail = _plain(result.detail)
        if result.status == RequirementStatus.UNMET:
            cited = "".join(self._citation(c) for c in result.clauses)
            body = f'<span class="verdict unmet">Failed.</span> {detail}.' + (
                f"<div><strong>Rule:</strong>{cited}</div>" if cited else ""
            )
            css = "unmet"
        elif result.status == RequirementStatus.PROVISIONAL:
            waiting = []
            if result.due:
                waiting.append(f"due {_long(result.due)}")
            if result.resolves_on and result.resolves_on != result.due:
                waiting.append(f"settles after {_long(result.resolves_on)}")
            reason = REASON_TEXT.get(result.reason, "") if result.reason else ""
            on = f" Waiting on: {escape('; '.join(waiting))}." if waiting else ""
            body = (
                f'<span class="verdict waiting">Waiting.</span> {detail}.'
                f"{on} Not yet judged because {escape(reason)}."
            )
            css = "waiting"
        else:
            body = f'<span class="verdict met">Met.</span> {detail}.'
            css = "met"
        items = [evidence[r] for r in result.evidence if r in evidence]
        shown = _evidence(items, folded=result.status == RequirementStatus.MET)
        return (
            f'<div class="req {css}"><div class="head">{name}</div><p>{body}</p>'
            f"{shown}{self._steps(result.derivation)}</div>"
        )

    def _bill(
        self,
        evaluation: TenureEvaluation,
        bill: BillRecord | None,
        evidence: Mapping[EvidenceRef, EvidenceItem],
    ) -> str:
        title, url = (bill.title, bill.url) if bill else (None, None)
        label = LABEL_TEXT[evaluation.label]
        bill_id = evaluation.bill.bill_id
        link = f' <a href="{escape(url)}">View the bill</a>' if url else ""
        search = escape(f"{bill_id} {title or ''}".lower())
        ordered = sorted(
            evaluation.results,
            key=lambda r: [
                RequirementStatus.UNMET,
                RequirementStatus.PROVISIONAL,
                RequirementStatus.MET,
            ].index(r.status),
        )
        return (
            f'<article class="bill" data-label="{label}" data-search="{search}">'
            f"<h3>{escape(_bill_label(bill_id))}: {escape(title or '')}</h3>"
            f'<div><span class="status {label}">{label}</span> as of '
            f"{escape(_long(evaluation.as_of.date()))}.{link}</div>"
            + "".join(self._requirement(r, evidence) for r in ordered)
            + f'<div class="cite">Tenure {escape(evaluation.tenure_id)}; inputs '
            f"{escape(evaluation.input_fingerprint[:12])}</div></article>"
        )

    def _owed_nothing(self, item: NoRequirements, bill: BillRecord | None) -> str:
        title = bill.title if bill else None
        bill_id = item.bill.bill_id
        text = NO_REQUIREMENTS_TEXT[item.reason]
        if item.carried_by is not None:
            came = f"Came before the committee {_long(item.referred_on)}"
            text = f"{text} {_bill_label(item.carried_by)}, which is checked instead."
        elif item.record_gap is not None:
            came = f"Came before the committee {_long(item.referred_on)}"
            text = f"{text} {item.record_gap}"
        else:
            came = f"Referred {_long(item.referred_on)}"
        if item.awaiting:
            names = ", ".join(REQUIREMENT_NAMES.get(a, a) for a in item.awaiting)
            text += f" Owed once the committee acts: {names}."
        return (
            f"<li><strong>{escape(_bill_label(bill_id))}</strong>: "
            f"{escape(title or '')}. {escape(came)}. "
            f"{escape(text)}</li>"
        )

    def _rule_table(self, evaluations: list[TenureEvaluation]) -> str:
        totals: dict[str, Counter[RequirementStatus]] = {}
        for e in evaluations:
            for r in e.results:
                totals.setdefault(r.requirement_id, Counter())[r.status] += 1
        rows = "".join(
            f"<tr><td>{escape(REQUIREMENT_NAMES.get(k, k))}</td>"
            f'<td class="met">{t[RequirementStatus.MET]}</td>'
            f'<td class="unmet">{t[RequirementStatus.UNMET]}</td>'
            f'<td class="waiting">{t[RequirementStatus.PROVISIONAL]}</td></tr>'
            for k, t in totals.items()
        )
        return (
            '<table class="rules"><thead><tr><th>Rule</th><th>Met</th><th>Failed</th>'
            f"<th>Waiting</th></tr></thead><tbody>{rows}</tbody></table>"
        )

    def write(self, report: ReportInput, out_dir: Path) -> list[Path]:
        run = run_identity(report)
        if run is None:
            return []
        evaluations = sorted(
            report.evaluations,
            key=lambda e: (
                _LABEL_ORDER[e.label],
                e.bill.bill_id[0],
                int(e.bill.bill_id[1:]),
            ),
        )
        bills = {b.bill_id: b for b in report.bills}
        counts = Counter(e.label for e in evaluations)
        count_html = "".join(
            f'<div class="count"><div class="n {LABEL_TEXT[label]}">'
            f"{counts[label]}</div>{LABEL_TEXT[label]}</div>"
            for label in (
                ComplianceLabel.NON_COMPLIANT,
                ComplianceLabel.PROVISIONAL,
                ComplianceLabel.COMPLIANT,
            )
        )
        name = escape(report.committee_name or report.committee_id)
        body = "".join(
            self._bill(e, bills.get(e.bill.bill_id), report.evidence)
            for e in evaluations
        )
        owed_nothing = sorted(
            report.no_requirements,
            key=lambda n: (n.bill.bill_id[0], int(n.bill.bill_id[1:]), n.tenure_id),
        )
        quiet = (
            '<h2>No applicable requirements</h2><p class="meta">These referrals '
            "owed nothing under the tracked rules as of this run. They carry no "
            'status and count toward no total.</p><ul class="quiet">'
            + "".join(
                self._owed_nothing(n, bills.get(n.bill.bill_id)) for n in owed_nothing
            )
            + "</ul>"
            if owed_nothing
            else ""
        )
        css = _STATIC.joinpath("audit.css").read_text(encoding="utf-8")
        script = _STATIC.joinpath("audit.js").read_text(encoding="utf-8")
        html = (
            f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>Audit report: {name}</title><style>{css}</style></head><body>"
            f'<div class="wrap"><header><div class="kicker">Compliance audit</div>'
            f'<h1>{name}</h1><div class="meta">Session {report.session}. As of '
            f"{escape(_long(run.as_of.date()))}. Ruleset {escape(run.ruleset_id)} "
            f"version {escape(run.ruleset_version)}. Evaluation run "
            f"{escape(str(run.run_id))}.</div></header>"
            f'<div class="counts">{count_html}</div><h2>Rule by rule</h2>'
            f"{self._rule_table(evaluations)}<h2>Every bill</h2>"
            '<div class="controls"><input id="q" type="search" '
            'placeholder="Search bill number or title" aria-label="Search">'
            '<select id="s" aria-label="Status"><option value="">All statuses</option>'
            "<option>Non-Compliant</option><option>Provisional</option>"
            "<option>Compliant</option></select>"
            '<span id="shown" class="meta"></span></div>'
            f"{body}{quiet}<footer>Each verdict cites the rule text it was judged "
            "against. "
            "Bills the committee produced itself (new drafts, study orders) are not "
            f"judged.</footer></div><script>{script}</script></body></html>"
        )
        stamp = run.as_of.date().isoformat()
        path = out_dir / stamp / f"{report.committee_id}_audit.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        return [path]

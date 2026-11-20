"""Compares v2 report-out results against a v1 basic_<committee>.json file."""

import json
from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

from beacon.domain.evaluation import RequirementResult, TenureEvaluation
from beacon.evaluator.rulesets.s194.requirements import S194Requirement
from beacon.reporters.base import ReportInput


class Agreement(StrEnum):
    """How a bill's v1 and v2 report-out data compare."""

    MATCH = "match"
    DUE_DIFFERS = "due_differs"
    COMPLETION_DIFFERS = "completion_differs"
    BOTH_DIFFER = "both_differ"
    ONLY_V1 = "only_v1"
    ONLY_V2 = "only_v2"


@dataclass(frozen=True)
class ParityRow:
    """One bill's comparison."""

    bill_id: str
    agreement: Agreement
    v1_state: str | None
    v1_due: str | None
    v1_reported: str | None
    v2_label: str | None
    v2_status: str | None
    v2_due: str | None
    v2_completed: str | None
    v2_detail: str | None


def _v1_date(value: object) -> str | None:
    if not isinstance(value, str) or value in ("", "None"):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _report_out(evaluation: TenureEvaluation) -> RequirementResult | None:
    return next(
        (
            r
            for r in evaluation.results
            if r.requirement_id == S194Requirement.REPORT_OUT_DEADLINE
        ),
        None,
    )


def _agreement(due_same: bool, completed_same: bool) -> Agreement:
    if due_same and completed_same:
        return Agreement.MATCH
    if completed_same:
        return Agreement.DUE_DIFFERS
    if due_same:
        return Agreement.COMPLETION_DIFFERS
    return Agreement.BOTH_DIFFER


def compare(report: ReportInput, v1_bills: list[dict]) -> list[ParityRow]:
    """One row per bill in either version, using each bill's latest v2 tenure."""
    latest: dict[str, TenureEvaluation] = {}
    for e in report.evaluations:
        latest[e.bill.bill_id] = e
    v1 = {b["bill_id"]: b for b in v1_bills}
    rows = []
    for bill_id in sorted(set(latest) | set(v1)):
        old, new = v1.get(bill_id), latest.get(bill_id)
        result = _report_out(new) if new else None
        v1_due = _v1_date(old.get("effective_deadline")) if old else None
        v1_reported = _v1_date(old.get("reported_out_date")) if old else None
        v2_due = result.due.isoformat() if result and result.due else None
        v2_completed = (
            result.completed_on.isoformat() if result and result.completed_on else None
        )
        if old is None:
            agreement = Agreement.ONLY_V2
        elif new is None:
            agreement = Agreement.ONLY_V1
        else:
            agreement = _agreement(v1_due == v2_due, v1_reported == v2_completed)
        rows.append(
            ParityRow(
                bill_id=bill_id,
                agreement=agreement,
                v1_state=old.get("state") if old else None,
                v1_due=v1_due,
                v1_reported=v1_reported,
                v2_label=new.label.value if new else None,
                v2_status=result.status.value if result else None,
                v2_due=v2_due,
                v2_completed=v2_completed,
                v2_detail=result.detail if result else None,
            )
        )
    return rows


def _markdown(report: ReportInput, rows: list[ParityRow], source: Path) -> str:
    counts: dict[Agreement, int] = {a: 0 for a in Agreement}
    for r in rows:
        counts[r.agreement] += 1
    lines = [
        f"# v1 parity: {report.committee_id} (session {report.session})",
        "",
        f"v1 source: `{source}`",
        "",
        "| Agreement | Bills |",
        "|---|---|",
        *(f"| {a.value} | {n} |" for a, n in counts.items()),
        "",
        "## Differences",
        "",
        "| Bill | Agreement | v1 due | v2 due | v1 reported | v2 completed | "
        "v2 status | v2 detail |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {r.bill_id} | {r.agreement.value} | {r.v1_due} | {r.v2_due} | "
        f"{r.v1_reported} | {r.v2_completed} | {r.v2_status} | {r.v2_detail} |"
        for r in rows
        if r.agreement != Agreement.MATCH
    )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class V1ParityReporter:
    """Writes parity.md and parity.json beside the JSON report."""

    v1_json: Path

    def write(self, report: ReportInput, out_dir: Path) -> list[Path]:
        if not report.evaluations:
            return []
        v1_bills = json.loads(self.v1_json.read_text(encoding="utf-8"))["bills"]
        rows = compare(report, v1_bills)
        stamp = report.evaluations[0].as_of.date().isoformat()
        base = out_dir / stamp
        base.mkdir(parents=True, exist_ok=True)
        md = base / f"{report.committee_id}_parity.md"
        js = base / f"{report.committee_id}_parity.json"
        md.write_text(_markdown(report, rows, self.v1_json), encoding="utf-8")
        js.write_text(json.dumps([asdict(r) for r in rows], indent=2), encoding="utf-8")
        return [md, js]

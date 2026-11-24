"""Full-fidelity JSON export of a committee's evaluations."""

import json
from collections import Counter
from pathlib import Path

from beacon.domain.evaluation import TenureEvaluation
from beacon.reporters.base import ReportInput, run_identity


def _tenure(evaluation: TenureEvaluation) -> dict:
    return {
        "tenure_id": evaluation.tenure_id,
        "label": evaluation.label.value,
        "input_fingerprint": evaluation.input_fingerprint,
        "requirements": [r.model_dump(mode="json") for r in evaluation.results],
    }


class JsonReporter:
    """Writes one JSON document per committee per run."""

    def write(self, report: ReportInput, out_dir: Path) -> list[Path]:
        run = run_identity(report)
        if run is None:
            return []
        by_bill: dict[str, list[TenureEvaluation]] = {}
        for e in report.evaluations:
            by_bill.setdefault(e.bill.bill_id, []).append(e)
        bills = {b.bill_id: b for b in report.bills}
        document = {
            "session": report.session,
            "committee_id": report.committee_id,
            "committee_name": report.committee_name,
            "ruleset": {"id": run.ruleset_id, "version": run.ruleset_version},
            "as_of": run.as_of.isoformat(),
            "evaluation_run": str(run.run_id),
            "summary": dict(Counter(e.label.value for e in report.evaluations)),
            "bills": [
                {
                    "bill_id": bill_id,
                    "title": bills[bill_id].title if bill_id in bills else None,
                    "url": bills[bill_id].url if bill_id in bills else None,
                    "tenures": [_tenure(e) for e in evaluations],
                }
                for bill_id, evaluations in sorted(by_bill.items())
            ],
            "committee_output": [
                {
                    "bill_id": o.bill_id,
                    "title": o.title,
                    "originated_on": o.originated_on.isoformat(),
                    "source_bills": list(o.source_bills),
                }
                for o in report.committee_output
            ],
            "no_requirements": [
                {
                    "tenure_id": n.tenure_id,
                    "bill_id": n.bill.bill_id,
                    "referred_on": n.referred_on.isoformat(),
                    "reason": n.reason.value,
                    "awaiting": list(n.awaiting),
                    "carried_by": n.carried_by,
                    "record_gap": n.record_gap,
                }
                for n in report.no_requirements
            ],
        }
        stamp = run.as_of.date().isoformat()
        path = out_dir / stamp / f"{report.committee_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        return [path]

"""The audit report explains every verdict and quotes the rules behind failures."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from beacon.evaluator.clauses import ClauseIndex
from beacon.orchestrator import stages
from beacon.orchestrator.stages import Context
from beacon.reporters.audit_html import AuditReporter
from tests.builders import ET
from tests.orchestrator.test_pipeline import ctx  # noqa: F401


def _report(context: Context, as_of: datetime | None = None) -> str:
    stages.collect(context, "J19")
    stages.scrape(context, "J19")
    stages.evaluate(context, "J19", as_of)
    (path,) = stages.report(context, "J19", [AuditReporter(context.clauses)])
    assert path.name == "J19_audit.html"
    return path.read_text(encoding="utf-8")


def test_failures_cite_rule_text(ctx: Context):  # noqa: F811
    html = _report(ctx)
    assert "H.1768" in html and "Non-Compliant" in html
    assert "Failed." in html
    assert "House Rule 27 para 2" in html
    assert "The House chair of each joint standing committee" in html
    assert "How this was worked out" in html
    assert "2025-06-07" not in html and "June 7, 2025" in html


def test_waiting_items_say_what_they_wait_on(ctx: Context):  # noqa: F811
    html = _report(ctx, datetime(2025, 7, 1, 12, tzinfo=ET))
    assert "Waiting." in html and "Waiting on: due" in html


def test_changed_rule_text_is_flagged(ctx: Context, tmp_path: Path):  # noqa: F811
    stages.collect(ctx, "J19")
    stages.scrape(ctx, "J19")
    stages.evaluate(ctx, "J19")
    rules = {
        source: {
            rule: tuple(p + " (edited)" for p in paras) for rule, paras in table.items()
        }
        for source, table in ctx.clauses.rules.items()
    }
    edited = AuditReporter(ClauseIndex(rules))
    (path,) = stages.report(replace(ctx), "J19", [edited])
    assert "The rule text has changed since this was evaluated." in path.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("label", ["Compliant", "Provisional", "Non-Compliant"])
def test_filter_options_present(ctx: Context, label: str):  # noqa: F811
    assert f"<option>{label}</option>" in _report(ctx)


def _article(html: str, bill_label: str) -> str:
    start = html.index(f"<h3>{bill_label}:")
    return html[start : html.index("</article>", start)]


def test_failed_requirement_shows_history_lines(ctx: Context):  # noqa: F811
    bill = _article(_report(ctx), "H.1768")
    assert '<div class="evidence-head">Evidence</div>' in bill
    assert "Bill history." in bill
    assert "Accompanied a new draft, see H4372" in bill
    assert 'href="https://malegislature.gov/Bills/194/H1768"' in bill


def test_met_evidence_is_folded_and_links_documents(ctx: Context):  # noqa: F811
    html = _report(ctx)
    assert "<summary>Evidence (" in html
    assert "Found the summary" in html or "Found the committee vote record" in html

"""Bill history extraction against captured J19 pages, through to evaluation."""

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from beacon.domain.captures import Capture
from beacon.domain.evaluation import RequirementStatus as S
from beacon.domain.ids import BillKey
from beacon.domain.observations import ActionType as A
from beacon.evaluator.clauses import ClauseIndex
from beacon.evaluator.engine import evaluate
from beacon.evaluator.facts import project
from beacon.evaluator.rulesets.s194.ruleset import RULESET
from beacon.scraper.history.extractor import (
    classify,
    extract,
    parse_history,
    related_bills,
)

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
AS_OF = datetime(2026, 11, 18, 12, tzinfo=ZoneInfo("America/New_York"))


def page(bill_id: str) -> bytes:
    return (PAGES / f"{bill_id}.html").read_bytes()


def capture_for(bill_id: str) -> Capture:
    return Capture(
        capture_id=uuid4(),
        run_id=uuid4(),
        url=f"https://malegislature.gov/Bills/194/{bill_id}",
        requested_at=datetime(2026, 11, 18, tzinfo=UTC),
        http_status=200,
        content_hash="0" * 64,
    )


def test_adverse_report_under_joint_rule_is_a_report():
    text = (
        "Proposal for Constitutional Amendment ought not to pass "
        "(under Joint Rule 23) and placed on file"
    )
    assert A.REPORTED in [t for t, _ in classify(text)]


@pytest.mark.parametrize(
    ("text", "committee_id"),
    [
        (
            "Reported, referred to the committee on Joint Rules, reported, rules "
            "suspended and referred to the committee on Public Health",
            "J16",
        ),
        (
            "Reported, referred to the committee on Joint Rules, reported, rules "
            "suspended, question divided (on motion of Fiola), and referred (on "
            "motion of Fiola) to the committee on Economic Development and "
            "Emerging Technologies",
            "J12",
        ),
    ],
)
def test_compound_referral_takes_the_last_committee(text: str, committee_id: str):
    referrals = [d for t, d in classify(text) if t == A.REFERRED]
    assert [d["committee_id"] for d in referrals] == [committee_id]


def test_classify_unknown_and_compound():
    assert classify("Something novel") == [(A.UNKNOWN, {})]
    kinds = [t for t, _ in classify("Accompanied a study order, see H5281")]
    assert A.STUDY_ORDER in kinds and A.ACCOMPANIED in kinds


def test_title_and_rows():
    payload = parse_history(page("H67"))
    assert payload.title is not None
    assert payload.title.startswith("Proposal for a legislative amendment")
    first = payload.rows[0]
    assert (first.action_date, first.action_type, first.committee_id) == (
        date(2025, 3, 3),
        A.REFERRED,
        "J19",
    )


def test_undated_rows_skipped_and_extensions_parsed():
    payload = parse_history(page("S1043"))
    deadlines = [r.new_deadline for r in payload.rows if r.new_deadline]
    assert deadlines == [date(2026, 3, 2), date(2026, 4, 2), date(2026, 6, 30)]
    assert len({r.row_index for r in payload.rows}) == 8


def test_extraction_is_deterministic_per_capture():
    capture = capture_for("H67")
    assert extract(capture, page("H67"), 194, "H67") == extract(
        capture, page("H67"), 194, "H67"
    )


@pytest.mark.parametrize(
    ("bill_id", "status", "due", "completed_on"),
    [
        ("H1768", S.UNMET, date(2025, 6, 7), date(2025, 8, 11)),
        ("H1648", S.UNMET, date(2025, 6, 7), date(2026, 3, 26)),
        ("H67", S.MET, date(2025, 6, 7), date(2025, 5, 1)),
        ("S1168", S.MET, date(2025, 12, 3), date(2025, 6, 16)),
        ("S1043", S.UNMET, date(2025, 12, 3), date(2026, 7, 22)),
        ("H4783", S.UNMET, date(2026, 6, 6), None),
    ],
)
def test_end_to_end_report_out(
    bill_id, status, due, completed_on, clause_index: ClauseIndex
):
    obs = extract(capture_for(bill_id), page(bill_id), 194, bill_id)
    facts = project(BillKey(194, bill_id), [obs])
    (evaluation,) = evaluate(facts, "J19", RULESET, clause_index, AS_OF, uuid4())
    (result,) = [
        r for r in evaluation.results if r.requirement_id == "report_out_deadline"
    ]
    assert (result.status, result.due, result.completed_on) == (
        status,
        due,
        completed_on,
    )


def test_related_bills():
    text = "Accompanied by H1581 , H1582 , S.12 and H 9; Section 5"
    assert related_bills(text, "H5281") == ("H1581", "H1582", "S12", "H9")
    assert related_bills("New draft of H1768", "H1768") == ()


def test_committee_documents_have_origin_and_no_evaluation(clause_index: ClauseIndex):
    for bill_id, sources in (("H4372", ("H1768",)), ("H5281", None)):
        obs = extract(capture_for(bill_id), page(bill_id), 194, bill_id)
        facts = project(BillKey(194, bill_id), [obs])
        assert facts.origin is not None
        if sources:
            assert facts.origin.source_bills == sources
        else:
            assert len(facts.origin.source_bills) > 300
        assert evaluate(facts, "J19", RULESET, clause_index, AS_OF, uuid4()) == []

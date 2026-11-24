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


def test_recommittal_is_a_referral():
    text = (
        "Recommitted to the committee on State Administration and Regulatory Oversight"
    )
    assert classify(text) == [
        (
            A.REFERRED,
            {
                "committee": "State Administration and Regulatory Oversight",
                "committee_name": "State Administration and Regulatory Oversight",
                "committee_id": "J25",
            },
        )
    ]


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


@pytest.mark.parametrize(
    ("text", "deadline"),
    [
        ("Reporting date extended to Friday, March 6, 2026", "2026-03-06"),
        ("Reporting date extended until Friday, November 7, 2025", "2025-11-07"),
        ("Reported date extended to Wednesday, March 18, 2026", "2026-03-18"),
        (
            "Reported date extended to Wednesday, March 18, 2026 "
            "(sections 45 to 72, inclusive, 85 and 86)",
            "2026-03-18",
        ),
    ],
)
def test_extension_wordings_are_extensions_not_reports(text: str, deadline: str):
    assert [(t, d["new_deadline"]) for t, d in classify(text)] == [
        (A.REPORTING_EXTENDED, deadline)
    ]


def test_bare_reported_is_still_a_report():
    assert A.REPORTED in [t for t, _ in classify("Reported favorably")]


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


def _table(*rows: tuple[str, str, str]) -> bytes:
    cells = "".join(
        f"<tr><td>{d}</td><td>{b}</td><td>{t}</td></tr>" for d, b, t in rows
    )
    return f"<table>{cells}</table>".encode()


def _of(rows, action_type):
    return [(r.row_index, r.committee_id) for r in rows if r.action_type == action_type]


@pytest.mark.parametrize(
    ("branch", "committee_id"), [("House", "H36"), ("Senate", "S31")]
)
def test_order_to_third_reading_refers_to_the_branch_committee(
    branch: str, committee_id: str
):
    rows = parse_history(
        _table(
            ("07/17/2025", branch, "Read second and ordered to a third reading"),
            (
                "09/18/2025",
                branch,
                "Read third (title changed) and passed to be engrossed",
            ),
        )
    ).rows
    assert _of(rows, A.REFERRED_TO_BILLS_IN_THIRD_READING) == [(0, committee_id)]
    assert _of(rows, A.READ_THIRD) == [(1, committee_id)]


@pytest.mark.parametrize(
    ("branch", "text", "committee_id"),
    [
        ("House", "Referred to the committee on Bills in the Third Reading", "H36"),
        ("Senate", "Referred to the committee on Bills in the Third Reading", "S31"),
        (
            "Senate",
            "Referred to the committee on Senate Bills in the Third Reading",
            "S31",
        ),
    ],
)
def test_named_third_reading_referral_follows_the_branch(
    branch: str, text: str, committee_id: str
):
    rows = parse_history(_table(("07/17/2025", branch, text))).rows
    assert {r.committee_id for r in rows if r.committee_id} == {committee_id}


@pytest.mark.parametrize(
    ("branch", "text", "committee_id"),
    [
        ("Senate", "Bills in the Third Reading report accepted", "S31"),
        ("Senate", "Bill in the Third Reading report accepted", "S31"),
        (
            "Senate",
            "Bills in the Third Reading report (consolidated with S545 ) accepted",
            "S31",
        ),
        (
            "Senate",
            "Bills in the Third Reading new draft substituted, see S3133",
            "S31",
        ),
        (
            "Senate",
            "Reported from the committee on Senate Bills in the Third Reading",
            "S31",
        ),
        ("House", "Bills in the Third Reading special report accepted", "H36"),
    ],
)
def test_third_reading_committee_reports(branch: str, text: str, committee_id: str):
    rows = parse_history(_table(("07/17/2025", branch, text))).rows
    assert _of(rows, A.REPORTED) == [(0, committee_id)]


def _rows(*cells: tuple[str, str, str]):
    body = "".join(
        f"<tr><td>{d}</td><td>{b}</td><td>{t}</td></tr>" for d, b, t in cells
    )
    return parse_history(f"<table>{body}</table>".encode(), "S1").rows


@pytest.mark.parametrize(
    ("text", "committee_id"),
    [
        (
            "Read; and referred to the House committee on Steering, Policy and "
            "Scheduling",
            "H52",
        ),
        (
            "Read; and referred to the temporary committee on House Ways and Means",
            "H34",
        ),
        (
            "Read; and referred to the committee on House Steering, Policy and "
            "Schedule",
            "H52",
        ),
        (
            "Discharged to the committee on State Administration and Reglatory "
            "Oversight",
            "J25",
        ),
    ],
)
def test_referral_variants_name_the_committee(text: str, committee_id: str):
    referrals = [
        r
        for r in _rows(("12/4/2025", "House", text))
        if r.action_type in (A.REFERRED, A.DISCHARGED)
    ]
    assert [r.committee_id for r in referrals] == [committee_id]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Read second, amended (as recommended by the committee on House Ways and "
            "Means)",
            [(A.READ_SECOND, None)],
        ),
        (
            "Read; and referred to the committee on House Steering, Policy and "
            "Scheduling",
            [(A.REFERRED, "H52"), (A.READ, None)],
        ),
    ],
)
def test_widened_referral_leaves_other_rows_alone(text: str, expected: list):
    rows = _rows(("7/31/2026", "House", text))
    assert [(r.action_type, r.committee_id) for r in rows] == expected


def test_house_committee_report_is_not_a_referral():
    rows = _rows(
        ("3/19/2026", "House", "Reported from the committee on House Ways and Means")
    )
    assert [(r.action_type, r.committee_id) for r in rows] == [(A.REPORTED, "H34")]

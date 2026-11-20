"""Document presence and absence windows from search observations."""

from datetime import date, datetime

from beacon.domain.ids import BillKey
from beacon.domain.observations import DocumentKind as D
from beacon.evaluator.facts import project
from tests.builders import ET, document, search

BILL = BillKey(194, "H1")


def at(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=ET)


def test_never_searched_is_none():
    assert project(BILL, []).document(D.VOTES, "J19") is None


def test_absent_then_present_bounds_posting():
    facts = project(
        BILL,
        [
            search("H1", D.VOTES, "2025-07-20T02:00"),
            search("H1", D.VOTES, "2025-07-21T02:00"),
            search("H1", D.VOTES, "2025-07-22T02:00", found=True),
            search("H1", D.VOTES, "2025-07-23T02:00", found=True),
        ],
    )
    evidence = facts.document(D.VOTES, "J19")
    assert evidence is not None
    assert evidence.first_seen_at == at("2025-07-22T02:00")
    assert evidence.absent_before == at("2025-07-21T02:00")


def test_non_exhaustive_empty_search_is_not_absence():
    facts = project(
        BILL,
        [
            search("H1", D.VOTES, "2025-07-21T02:00", exhaustive=False),
            search("H1", D.VOTES, "2025-07-22T02:00", found=True),
        ],
    )
    evidence = facts.document(D.VOTES, "J19")
    assert evidence is not None and evidence.absent_before is None


def test_never_found_keeps_latest_absence():
    facts = project(
        BILL,
        [
            search("H1", D.SUMMARY, "2025-03-01T02:00"),
            search("H1", D.SUMMARY, "2025-03-09T02:00"),
        ],
    )
    evidence = facts.document(D.SUMMARY, "J19")
    assert evidence is not None
    assert evidence.first_seen_at is None
    assert evidence.absent_before == at("2025-03-09T02:00")


def test_document_details_and_committee_separation():
    facts = project(
        BILL,
        [
            search("H1", D.VOTES, "2025-07-22T02:00", found=True),
            document(
                "H1",
                D.VOTES,
                "2025-07-22T02:00",
                created_at="2025-07-21T15:00",
                vote_date="2025-07-21",
            ),
            search("H1", D.VOTES, "2025-07-22T02:00", committee_id="J33"),
        ],
    )
    j19 = facts.document(D.VOTES, "J19")
    assert j19 is not None
    assert j19.vote_date == date(2025, 7, 21)
    assert j19.created_at == at("2025-07-21T15:00")
    j33 = facts.document(D.VOTES, "J33")
    assert j33 is not None and j33.first_seen_at is None

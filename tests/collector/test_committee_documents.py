"""Tests for the committee Documents tab and document classification."""

from datetime import date
from pathlib import Path

import pytest

from beacon.collector.committee_documents import (
    ListedDocument,
    committee_document,
    documents_url,
    parse_documents_tab,
)
from beacon.domain.observations import DocumentKind

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
BASE = "https://malegislature.gov"


def listed(title: str) -> ListedDocument:
    return ListedDocument(f"{BASE}/Reports/1/doc.pdf", title, None)


def read(fixture: str) -> bytes:
    return (PAGES / fixture).read_bytes()


def test_documents_url():
    assert documents_url(BASE, "J17", 194) == (
        f"{BASE}/Committees/Detail/J17/194/Documents"
    )


def test_documents_tab_rows():
    documents = parse_documents_tab(read("J17_Documents.html"), BASE)
    assert len(documents) == 16
    assert documents[0] == ListedDocument(
        f"{BASE}/Reports/23967/CPPL%20Poll%20Results%207.7.25.pdf",
        "CPPL Poll Results 7.7.25",
        date(2025, 7, 9),
    )


def test_empty_documents_tab():
    assert parse_documents_tab(read("J19_Documents.html"), BASE) == ()


def test_poll_results_cover_rows_and_attached_bills():
    doc = committee_document(
        listed("CPPL Poll Results 7.7.25"), read("report_23967.pdf")
    )
    assert doc.document_kind == DocumentKind.VOTES
    assert doc.from_content
    assert {"H318", "H334", "H388"} <= set(doc.bill_ids)
    assert {"H436", "H410", "H413"} <= set(doc.bill_ids)
    assert {"H317", "H471"} <= set(doc.bill_ids)
    assert "H27" not in doc.bill_ids
    assert doc.created_at is not None and doc.created_at.date() == date(2025, 7, 9)


@pytest.mark.parametrize(
    ("title", "fixture", "bills"),
    [
        ("7-2-25 MRG poll results", "report_28649.pdf", {"H2243", "H4205"}),
        ("Election Laws Poll 1 Results (7.2.25)", "report_29790.docx", {"H801"}),
        ("H3443", "report_30793.pdf", {"H3443"}),
    ],
)
def test_vote_formats(title, fixture, bills):
    doc = committee_document(listed(title), read(fixture))
    assert doc.document_kind == DocumentKind.VOTES
    assert bills <= set(doc.bill_ids)


def test_bare_bill_title_is_vote_only_with_tally_text():
    doc = committee_document(listed("H3443"), None)
    assert doc.document_kind is None and doc.bill_ids == ()


def test_summary_from_title_without_reading():
    doc = committee_document(listed("H.2253-S.1458_summary_Cahill-Montigny"), None)
    assert doc.document_kind == DocumentKind.SUMMARY
    assert doc.bill_ids == ("H2253", "S1458")
    assert not doc.from_content


def test_summary_title_wins_over_vote_text():
    doc = committee_document(listed("H3443 Summary"), read("report_30793.pdf"))
    assert doc.document_kind == DocumentKind.SUMMARY


@pytest.mark.parametrize(
    "title", ["JCFS Public Schedule of Hearings", "25-26 JCOT Rules"]
)
def test_other_documents_cover_no_bills(title):
    doc = committee_document(listed(title), b"")
    assert doc.document_kind is None and doc.bill_ids == ()


def test_unreadable_vote_titled_document_uses_title_bills():
    doc = committee_document(listed("H1358 - Votes of Committee Members"), b"")
    assert doc.document_kind == DocumentKind.VOTES
    assert doc.bill_ids == ("H1358",)
    assert not doc.from_content

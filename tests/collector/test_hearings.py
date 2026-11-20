"""Tests for hearing list, hearing page, and packet parsing."""

from datetime import date
from pathlib import Path

from beacon.adapters.documents import read_document
from beacon.collector.hearings import (
    packet_bills,
    parse_hearing,
    parse_hearing_list,
)

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
BASE = "https://malegislature.gov"


def test_hearing_list():
    ids = parse_hearing_list((PAGES / "J19_hearings.html").read_bytes())
    assert len(ids) == 16
    assert "5201" in ids


def test_hearing_with_per_bill_summaries():
    page = parse_hearing((PAGES / "hearing_5123.html").read_bytes(), BASE, "5123")
    assert page.hearing_date == date(2025, 4, 8)
    assert "H1648" in page.docket
    by_title = {d.title: d for d in page.documents}
    summary = by_title["H1648 Bill Summary"]
    assert summary.title_bills == ("H1648",)
    assert not summary.needs_content
    assert summary.url.startswith(f"{BASE}/Events/DownloadDocument?attachmentId=")


def test_hearing_with_packets():
    page = parse_hearing((PAGES / "hearing_5201.html").read_bytes(), BASE, "5201")
    assert page.hearing_date == date(2025, 6, 3)
    assert len(page.docket) == 62
    titles = {d.title for d in page.documents if d.needs_content}
    assert titles == {
        "JCOJ Hearing 4 Packet",
        "Hearing #4 House Bill Summaries",
        "Judiciary 6.3.25 Senate Summaries",
    }


def test_packet_headers_restricted_to_docket():
    page = parse_hearing((PAGES / "hearing_5201.html").read_bytes(), BASE, "5201")
    house = read_document((PAGES / "doc_22097.pdf").read_bytes())
    senate = read_document((PAGES / "doc_22098.pdf").read_bytes())
    assert house.created_at is not None and house.created_at.date() == date(2025, 5, 30)
    house_bills = packet_bills(house.text, page.docket)
    senate_bills = packet_bills(senate.text, page.docket)
    assert "H1594" in house_bills
    assert all(b.startswith("H") for b in house_bills)
    assert senate_bills and all(b.startswith("S") for b in senate_bills)
    assert set(house_bills) | set(senate_bills) <= set(page.docket)


def test_unreadable_pdf_is_empty():
    content = read_document(b"%PDF-broken")
    assert content.created_at is None and content.text == ""


def test_docx_packet():
    content = read_document((PAGES / "doc_packet10.docx").read_bytes())
    assert content.created_at is not None
    assert content.created_at.isoformat() == "2025-09-19T15:52:00+00:00"
    docket = ("H1595", "H1607", "H9999")
    assert packet_bills(content.text, docket) == ("H1595", "H1607")


def test_unknown_format_is_empty():
    assert read_document(b"<html>").text == ""

"""Tests for the bill-tab document finder and vote tab parser."""

from datetime import date
from pathlib import Path

from beacon.domain.observations import DocumentKind as D
from beacon.scraper.documents.bill_tabs import (
    parse_vote_tab,
    report_vote_links,
    summary_documents_for,
    tab_candidates,
)

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
BASE = "https://malegislature.gov/Bills/194"


def page(name: str) -> bytes:
    return (PAGES / f"{name}.html").read_bytes()


def test_house_bill_with_committee_summary_and_vote():
    found = tab_candidates(page("H1768"), f"{BASE}/H1768")
    assert found[D.SUMMARY].url == f"{BASE}/H1768/CommitteeSummary"
    assert found[D.VOTES].url == f"{BASE}/H1768/CommitteeVote"


def test_senate_bill_with_sponsor_summary_only():
    found = tab_candidates(page("S1168"), f"{BASE}/S1168")
    assert found[D.SUMMARY].location == "Primary Sponsor Summary tab"
    assert D.VOTES not in found


def test_vote_panel():
    (vote,) = parse_vote_tab(page("H1768_CommitteeVote"))
    assert vote.committee_id == "J19"
    assert vote.vote_date == date(2025, 7, 21)
    assert vote.motion is not None and vote.motion.startswith("H1768")
    assert vote.tallies == {
        "Favorable": 11,
        "Adverse": 0,
        "Reserve Right": 0,
        "No Action": 0,
    }
    assert len(vote.records) == 11
    assert vote.records[0].member == "Donald R. Berthiaume, Jr."
    assert {r.vote for r in vote.records} == {"Favorable"}


def test_empty_panel_is_not_a_vote_record():
    body = (
        b'<div class="panel committeeVote"><div class="panel-heading">'
        b"<h4>Joint Committee on the Judiciary</h4></div></div>"
    )
    (vote,) = parse_vote_tab(body)
    assert vote.committee_id == "J19"
    assert not vote.is_recorded
    (recorded,) = parse_vote_tab(page("H1768_CommitteeVote"))
    assert recorded.is_recorded


def test_report_vote_links():
    body = (
        b'<a href="/Reports">Reports and Communications</a>'
        b'<a href="/Bills/194/S1168.pdf">Download PDF</a>'
        b'<a href="/Reports/22939/Committee%20-%20Senate%20member%20votes.pdf">'
        b"Committee - Senate member votes</a>"
    )
    links = report_vote_links(body, "https://malegislature.gov")
    assert links == [
        (
            "https://malegislature.gov/Reports/22939/Committee%20-%20Senate%20member%20votes.pdf",
            "Committee - Senate member votes",
        )
    ]


def test_summary_documents_on_accompanying_tab():
    body = page("H5056_CommitteeSummary")
    found = summary_documents_for(body, "H1646", "https://malegislature.gov")
    assert len(found) == 1 and "H1646%20Bill%20Summary" in found[0]
    assert summary_documents_for(body, "H164", "https://malegislature.gov") == []

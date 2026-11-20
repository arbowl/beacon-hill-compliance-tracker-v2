"""Tests for parsing the committee bill listing."""

from pathlib import Path

from beacon.collector.committee_bills import listing_url, parse_listing

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
BASE = "https://malegislature.gov"


def parse(n: int):
    return parse_listing((PAGES / f"J19_bills_p{n}.html").read_bytes(), BASE, 194)


def test_first_page():
    page = parse(1)
    assert len(page.bills) == 25
    assert page.last_page == 34
    first = page.bills[0]
    assert first.bill_id == "S9"
    assert first.url == f"{BASE}/Bills/194/S9"
    assert first.title is not None and first.title.startswith("Proposal for")


def test_last_page_ignores_links_inside_rows():
    page = parse(34)
    assert len(page.bills) == 21
    assert page.bills[-1].bill_id == "H5281"


def test_past_last_page_is_empty():
    assert parse(35).bills == ()


def test_listing_urls():
    assert "current=True" in listing_url(BASE, 194, "J19", 1, True)
    url = listing_url(BASE, 194, "J19", 2, False)
    assert "current=False" in url and url.endswith("pageNumber=2")


def test_current_listing_is_disjoint_from_departed():
    current = parse_listing(
        (PAGES / "J19_bills_current_p1.html").read_bytes(), BASE, 194
    )
    departed = {b.bill_id for n in (1, 34) for b in parse(n).bills}
    assert len(current.bills) == 23
    assert current.last_page == 1
    assert not {b.bill_id for b in current.bills} & departed

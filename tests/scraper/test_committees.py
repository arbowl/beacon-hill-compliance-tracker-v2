"""Committee names that were reused resolve by the date they were used."""

from datetime import date

from beacon.scraper.history.committees import normalize_committee_name
from beacon.scraper.history.extractor import parse_history


def test_shared_name_resolves_by_date():
    assert normalize_committee_name("Agriculture", date(2025, 2, 27)) == "J38"
    assert normalize_committee_name("Agriculture", date(2025, 9, 4)) == "J45"
    assert (
        normalize_committee_name("Agriculture and Fisheries", date(2025, 2, 27))
        == "J45"
    )


def test_history_rows_use_their_own_date():
    body = (
        b"<table><tr><td>2/27/2025</td><td>House</td>"
        b"<td>Referred to the committee on Agriculture</td></tr>"
        b"<tr><td>9/4/2025</td><td>House</td>"
        b"<td>Referred to the committee on Agriculture</td></tr></table>"
    )
    rows = parse_history(body, "H1").rows
    assert [r.committee_id for r in rows] == ["J38", "J45"]

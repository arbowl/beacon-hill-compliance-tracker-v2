"""Committee names that were reused resolve by the date they were used."""

from datetime import date

from beacon.scraper.history.committees import get_committee, normalize_committee_name
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


def test_name_split_across_lines_still_matches():
    text = (
        "Senators Votes \u2013 Municipalities and Regional \nGovernment \n"
        "Bill Number: S23"
    )
    assert get_committee("J10").matches(text)


def test_rules_of_the_two_branches_is_joint_rules():
    body = (
        b"<table><tr><td>2/11/2026</td><td>House</td>"
        b"<td>Bill reported favorably by committee and referred to the committee on "
        b"Rules of the two branches, acting concurrently</td></tr>"
        b"<tr><td>2/12/2026</td><td>House</td>"
        b"<td>Bill reported favorably by committee and referred to the committees on "
        b"Rules of the two branches, acting concurrently</td></tr>"
        b"<tr><td>3/5/2026</td><td>House</td>"
        b"<td>Discharged to the committee on House Rules</td></tr></table>"
    )
    referrals = [
        (r.action_type.value, r.committee_id)
        for r in parse_history(body, "H289").rows
        if r.committee_id
    ]
    assert referrals == [
        ("REFERRED", "J40"),
        ("REFERRED", "J40"),
        ("DISCHARGED", "H33"),
    ]


def test_trailing_clause_does_not_override_the_named_committee():
    assert (
        normalize_committee_name(
            "House Ways and Means, with the amendment previously recommended by "
            "the committee on Health Care Financing pending"
        )
        == "H34"
    )


def test_known_misspellings_resolve_exactly():
    assert (
        normalize_committee_name("State Administration and Reglatory Oversight")
        == "J25"
    )
    assert normalize_committee_name("House Steering, Policy and Schedule") == "H52"


def test_misspellings_never_match_as_substrings():
    assert normalize_committee_name("Reglatory Oversight") is None
    assert normalize_committee_name("Policy and Schedule") is None
    assert normalize_committee_name("Senate Steering, Policy and Schedule") is None


def test_elder_affairs_resolves_until_the_joint_rules():
    assert normalize_committee_name("Elder Affairs", date(2025, 2, 27)) == "J20"
    assert normalize_committee_name("Elder Affairs", date(2025, 6, 27)) is None
    assert (
        normalize_committee_name("Aging and Independence", date(2025, 6, 27)) == "J46"
    )


def test_elder_affairs_bill_moves_to_aging_and_independence():
    body = (
        b"<table><tr><td>2/27/2025</td><td>House</td>"
        b"<td>Referred to the committee on Elder Affairs</td></tr>"
        b"<tr><td>6/27/2025</td><td>House</td>"
        b"<td>Referred, pursuant to an order adopted by the two branches, to the "
        b"committee on Aging and Independence</td></tr></table>"
    )
    rows = parse_history(body, "H1352").rows
    assert [r.committee_id for r in rows] == ["J20", "J46"]

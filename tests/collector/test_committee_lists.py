"""Committee listings, and the name table kept in step with them."""

from pathlib import Path

import pytest

from beacon.collector.committees import parse_committee_list
from beacon.domain.ids import Chamber
from beacon.scraper.history.committees import COMMITTEE_REGISTRY

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
BASE = "https://malegislature.gov"


@pytest.mark.parametrize(
    ("chamber", "fixture", "count"),
    [
        (Chamber.JOINT, "committees_joint.html", 33),
        (Chamber.HOUSE, "committees_house.html", 10),
        (Chamber.SENATE, "committees_senate.html", 11),
    ],
)
def test_listing_matches_name_table(chamber: Chamber, fixture: str, count: int):
    records = parse_committee_list((PAGES / fixture).read_bytes(), BASE, 194, chamber)
    assert len(records) == count
    assert all(r.chamber == chamber for r in records)
    listed = {r.committee_id for r in records}
    prefix = chamber.value[0]
    current = {
        k
        for k, v in COMMITTEE_REGISTRY.items()
        if k.startswith(prefix) and v.active_until is None
    }
    assert listed == current


def test_canonical_names():
    records = parse_committee_list(
        (PAGES / "committees_joint.html").read_bytes(), BASE, 194, Chamber.JOINT
    )
    j45 = next(r for r in records if r.committee_id == "J45")
    assert j45.name == "Joint Committee on Agriculture and Fisheries"
    assert j45.url == f"{BASE}/Committees/Detail/J45"

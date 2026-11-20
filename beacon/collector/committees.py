"""Parses the Legislature's committee listings for each chamber."""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from beacon.domain.ids import Chamber
from beacon.domain.inventory import CommitteeRecord
from beacon.scraper.history.committees import get_committee

LISTINGS = {
    Chamber.JOINT: "/Committees/Joint",
    Chamber.HOUSE: "/Committees/House",
    Chamber.SENATE: "/Committees/Senate",
}
_DETAIL = re.compile(r"/Committees/Detail/([HSJ]\d+)/?$")


def listing_url(base_url: str, chamber: Chamber) -> str:
    """The committee listing page for a chamber."""
    return urljoin(base_url, LISTINGS[chamber])


def parse_committee_list(
    body: bytes, base_url: str, session: int, chamber: Chamber
) -> list[CommitteeRecord]:
    """Committees linked from a chamber's listing, named canonically when known."""
    soup = BeautifulSoup(body, "html.parser")
    found: dict[str, CommitteeRecord] = {}
    for link in soup.select('a[href*="/Committees/Detail/"]'):
        href = link.get("href")
        match = _DETAIL.search(href) if isinstance(href, str) else None
        text = " ".join(link.get_text(" ", strip=True).split())
        if match is None or not text or match.group(1) in found:
            continue
        committee_id = match.group(1)
        if Chamber.from_prefix(committee_id[0]) != chamber:
            continue
        alias = get_committee(committee_id)
        found[committee_id] = CommitteeRecord(
            session=session,
            committee_id=committee_id,
            name=alias.canonical_name if alias else text,
            chamber=chamber,
            url=urljoin(base_url, f"/Committees/Detail/{committee_id}"),
        )
    return sorted(
        found.values(), key=lambda c: (c.committee_id[0], int(c.committee_id[1:]))
    )

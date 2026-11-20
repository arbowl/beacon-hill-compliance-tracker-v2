"""Parses a committee's paginated bill listing."""

import re
from dataclasses import dataclass
from urllib.parse import urljoin
from uuid import NAMESPACE_URL, uuid5

from bs4 import BeautifulSoup

from beacon.domain.captures import Capture
from beacon.domain.ids import normalize_bill_id
from beacon.domain.observations import CommitteeBillListPayload, Observation

EXTRACTOR_ID = "committee_bill_list"
EXTRACTOR_VERSION = 1
LISTING_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
_BILL_HREF = re.compile(r"/Bills/(\d+)/([HS]\d+)", re.I)
_PAGE_LINK = re.compile(r"reloadAjaxContent\((\d+)")


@dataclass(frozen=True)
class ListedBill:
    """One row of a committee's bill listing."""

    bill_id: str
    url: str
    title: str | None


@dataclass(frozen=True)
class ListingPage:
    """The bills on one listing page and the highest page number linked."""

    bills: tuple[ListedBill, ...]
    last_page: int


def listing_url(
    base_url: str, session: int, committee_id: str, page: int, current: bool
) -> str:
    """URL of one listing page: bills in committee (current) or that have left it."""
    path = (
        f"/Committees/Detail/{committee_id}/{session}/Bills/asc/EntityNumber/"
        f"?current={current}&pageNumber={page}"
    )
    return urljoin(base_url, path)


def parse_listing(body: bytes, base_url: str, session: int) -> ListingPage:
    """Read the bill cell of each table row; links elsewhere in a row are ignored."""
    soup = BeautifulSoup(body, "html.parser")
    bills: list[ListedBill] = []
    for tr in soup.select("table tbody tr"):
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        link = cells[1].find("a", href=_BILL_HREF)
        href = link.get("href") if link else None
        match = _BILL_HREF.search(href) if isinstance(href, str) else None
        if match is None or int(match.group(1)) != session:
            continue
        title = " ".join(cells[2].get_text(" ", strip=True).split()) or None
        bills.append(
            ListedBill(
                normalize_bill_id(match.group(2)), urljoin(base_url, href), title
            )
        )
    pages = [int(n) for n in _PAGE_LINK.findall(body.decode("utf-8", "replace"))]
    return ListingPage(tuple(bills), max(pages, default=1))


def extract(
    capture: Capture, page: ListingPage, session: int, committee_id: str
) -> Observation:
    """One observation per listing capture."""
    key = f"{capture.capture_id}|{EXTRACTOR_ID}|{EXTRACTOR_VERSION}"
    return Observation(
        observation_id=uuid5(NAMESPACE_URL, key),
        capture_id=capture.capture_id,
        observed_at=capture.requested_at,
        extractor_id=EXTRACTOR_ID,
        extractor_version=EXTRACTOR_VERSION,
        session=session,
        committee_id=committee_id,
        payload=CommitteeBillListPayload(bill_ids=tuple(b.bill_id for b in page.bills)),
    )

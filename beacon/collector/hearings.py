"""Parses a committee's hearings list and hearing detail pages."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import NAMESPACE_URL, uuid5

from bs4 import BeautifulSoup

from beacon.adapters.documents import read_document
from beacon.domain.captures import Capture
from beacon.domain.ids import bill_ids_in, normalize_bill_id
from beacon.domain.observations import HearingDocument, HearingPayload, Observation

EXTRACTOR_ID = "hearing"
EXTRACTOR_VERSION = 3
_HEARING_HREF = re.compile(r"/Events/Hearings/Detail/(\d+)")
_BILL_HREF = re.compile(r"/Bills/\d+/([HS]\d+)", re.I)
_EVENT_DATE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"[A-Za-z]+\s+\d{1,2},\s+\d{4}"
)
_PACKET_HINT = re.compile(r"summar|packet", re.I)
_HOUSE_HEADER = re.compile(r"BILL\s+NO\.?\s+([HS])\.?\s?(\d{1,5})\b", re.I)
_SENATE_HEADER = re.compile(
    r"BILL\s+NUMBER\s+(Senate|House)\s*,\s*No\.?\s*(\d{1,5})\b", re.I
)


@dataclass(frozen=True)
class ListedDocument:
    """A document link on a hearing page."""

    url: str
    title: str
    title_bills: tuple[str, ...]

    @property
    def needs_content(self) -> bool:
        """A summary packet naming no bill in its title must be read."""
        return not self.title_bills and bool(_PACKET_HINT.search(self.title))


@dataclass(frozen=True)
class HearingPage:
    """What a hearing detail page lists."""

    hearing_id: str
    hearing_date: date | None
    docket: tuple[str, ...]
    documents: tuple[ListedDocument, ...]


def hearings_url(base_url: str, committee_id: str) -> str:
    """The committee's hearings tab."""
    return urljoin(base_url, f"/Committees/Detail/{committee_id}/Hearings")


def hearing_url(base_url: str, hearing_id: str) -> str:
    """A hearing's detail page."""
    return urljoin(base_url, f"/Events/Hearings/Detail/{hearing_id}")


def parse_hearing_list(body: bytes) -> list[str]:
    """Hearing ids linked from the hearings tab, in page order."""
    ids = _HEARING_HREF.findall(body.decode("utf-8", "replace"))
    return list(dict.fromkeys(ids))


def parse_hearing(body: bytes, base_url: str, hearing_id: str) -> HearingPage:
    """Date, docket, and document links of a hearing detail page."""
    soup = BeautifulSoup(body, "html.parser")
    match = _EVENT_DATE.search(soup.get_text(" ", strip=True))
    hearing_date = (
        datetime.strptime(match.group(0), "%A, %B %d, %Y").date() if match else None
    )
    docket = []
    for link in soup.select('a[href*="/Bills/"]'):
        href = link.get("href")
        found = _BILL_HREF.search(href) if isinstance(href, str) else None
        if found:
            docket.append(normalize_bill_id(found.group(1)))
    documents = []
    for link in soup.select('a[href*="DownloadDocument"]'):
        href = link.get("href")
        if not isinstance(href, str):
            continue
        query = parse_qs(urlsplit(href).query)
        title = (query.get("Title") or [link.get_text(" ", strip=True)])[0]
        documents.append(
            ListedDocument(urljoin(base_url, href), title, bill_ids_in(title))
        )
    unique = {d.url: d for d in documents}
    return HearingPage(
        hearing_id, hearing_date, tuple(dict.fromkeys(docket)), tuple(unique.values())
    )


def packet_bills(text: str, docket: tuple[str, ...]) -> tuple[str, ...]:
    """Bills with a summary header in a packet's text, restricted to the docket."""
    found = [
        f"{m.group(1).upper()}{int(m.group(2))}" for m in _HOUSE_HEADER.finditer(text)
    ]
    found += [
        f"{m.group(1)[0].upper()}{int(m.group(2))}"
        for m in _SENATE_HEADER.finditer(text)
    ]
    on_docket = set(docket)
    return tuple(b for b in dict.fromkeys(found) if b in on_docket)


def extract(
    capture: Capture,
    page: HearingPage,
    documents: tuple[HearingDocument, ...],
    consulted: tuple[Capture, ...],
    session: int,
    committee_id: str,
) -> Observation:
    """One observation per hearing page capture."""
    key = f"{capture.capture_id}|{EXTRACTOR_ID}|{EXTRACTOR_VERSION}"
    return Observation(
        observation_id=uuid5(NAMESPACE_URL, key),
        capture_id=capture.capture_id,
        observed_at=capture.requested_at,
        extractor_id=EXTRACTOR_ID,
        extractor_version=EXTRACTOR_VERSION,
        session=session,
        committee_id=committee_id,
        payload=HearingPayload(
            hearing_id=page.hearing_id,
            hearing_date=page.hearing_date,
            docket=page.docket,
            documents=documents,
            consulted=tuple(c.capture_id for c in consulted),
        ),
    )


def hearing_document(
    listed: ListedDocument, docket: tuple[str, ...], body: bytes | None
) -> HearingDocument:
    """The bills a document covers: from its title, or from a packet's text."""
    if body is None:
        return HearingDocument(
            url=listed.url, title=listed.title, bill_ids=listed.title_bills
        )
    content = read_document(body)
    bill_ids = (
        packet_bills(content.text, docket)
        if listed.needs_content
        else listed.title_bills
    )
    return HearingDocument(
        url=listed.url,
        title=listed.title,
        bill_ids=bill_ids,
        from_content=listed.needs_content,
        created_at=content.created_at,
    )

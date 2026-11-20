"""Parses a committee's Documents tab and sorts its documents into summaries and votes.

Committees post poll results and vote tallies here, and some post bill summaries.
A title alone is not enough: some committees title vote records with only the
bill number. Vote records are recognized by their title or by tally wording in
their text, and cover the bills their text names. Summaries are recognized by
title only and cover the bills their title names. Anything else covers no bills.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin
from uuid import NAMESPACE_URL, uuid5

from bs4 import BeautifulSoup, Tag

from beacon.adapters.documents import read_document
from beacon.domain.captures import Capture
from beacon.domain.ids import bill_ids_in
from beacon.domain.observations import (
    CommitteeDocument,
    CommitteeDocumentsPayload,
    DocumentKind,
    Observation,
)
from beacon.scraper.documents.bill_tabs import VOTE_WORDING

EXTRACTOR_ID = "committee_documents"
EXTRACTOR_VERSION = 1
_REPORT_HREF = re.compile(r"/Reports/\d+/", re.I)
_SUMMARY_WORDING = re.compile(r"summar", re.I)
_TALLY_WORDING = re.compile(
    r"\breserve\s+rights?\b|\bvia\s+poll\b|\bpoll\s+(?:tally|results)\b", re.I
)


@dataclass(frozen=True)
class ListedDocument:
    """A row of the Documents tab."""

    url: str
    title: str
    posted_on: date | None


def documents_url(base_url: str, committee_id: str, session: int) -> str:
    """The committee's Documents tab."""
    return urljoin(base_url, f"/Committees/Detail/{committee_id}/{session}/Documents")


def _cell_text(cell: Tag) -> str:
    return " ".join(cell.get_text(" ", strip=True).split())


def _posted_on(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError:
        return None


def parse_documents_tab(body: bytes, base_url: str) -> tuple[ListedDocument, ...]:
    """Each report linked from the tab, with its title and posting date."""
    soup = BeautifulSoup(body, "html.parser")
    listed: dict[str, ListedDocument] = {}
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not isinstance(href, str) or not _REPORT_HREF.search(href):
            continue
        row = link.find_parent("tr")
        cells = row.find_all("td") if row else []
        label = link.get("aria-label")
        fallback = re.sub(r"^Download\s+", "", label) if isinstance(label, str) else ""
        title = _cell_text(cells[1]) if len(cells) > 1 else fallback
        posted = _posted_on(_cell_text(cells[0])) if cells else None
        url = urljoin(base_url, href)
        listed.setdefault(url, ListedDocument(url, title, posted))
    return tuple(listed.values())


def committee_document(listed: ListedDocument, body: bytes | None) -> CommitteeDocument:
    """Classify one listed document; body is None when it could not be fetched."""
    content = read_document(body) if body is not None else None
    text = content.text if content else ""
    title_bills = bill_ids_in(listed.title)
    if _SUMMARY_WORDING.search(listed.title):
        kind, bill_ids, from_content = DocumentKind.SUMMARY, title_bills, False
    elif VOTE_WORDING.search(listed.title) or _TALLY_WORDING.search(text):
        bill_ids = tuple(dict.fromkeys(title_bills + bill_ids_in(text)))
        kind, from_content = DocumentKind.VOTES, bool(text)
    else:
        kind, bill_ids, from_content = None, (), False
    return CommitteeDocument(
        url=listed.url,
        title=listed.title,
        posted_on=listed.posted_on,
        document_kind=kind,
        bill_ids=bill_ids,
        from_content=from_content,
        created_at=content.created_at if content else None,
    )


def extract(
    capture: Capture,
    documents: tuple[CommitteeDocument, ...],
    consulted: tuple[Capture, ...],
    session: int,
    committee_id: str,
) -> Observation:
    """One observation per Documents tab capture."""
    key = f"{capture.capture_id}|{EXTRACTOR_ID}|{EXTRACTOR_VERSION}"
    return Observation(
        observation_id=uuid5(NAMESPACE_URL, key),
        capture_id=capture.capture_id,
        observed_at=capture.requested_at,
        extractor_id=EXTRACTOR_ID,
        extractor_version=EXTRACTOR_VERSION,
        session=session,
        committee_id=committee_id,
        payload=CommitteeDocumentsPayload(
            documents=documents, consulted=tuple(c.capture_id for c in consulted)
        ),
    )

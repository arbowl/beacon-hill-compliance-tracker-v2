"""Finds summary and vote documents linked from a bill page's tabs."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from beacon.domain.observations import DocumentCandidate, DocumentKind, VoteRecord
from beacon.scraper.history.committees import normalize_committee_name

FINDER_ID = "bill_tabs"
FINDER_VERSION = 9
_TABS = {
    "CommitteeSummary": (DocumentKind.SUMMARY, "Committee Summary tab"),
    "PrimarySponsorSummary": (DocumentKind.SUMMARY, "Primary Sponsor Summary tab"),
    "CommitteeVote": (DocumentKind.VOTES, "Committee Vote tab"),
}
_TALLY = re.compile(r"^\s*(.+?):\s*(\d+)\s*$")


@dataclass(frozen=True)
class CommitteeVote:
    """One committee's panel on a Committee Vote tab."""

    committee_id: str | None
    committee_name: str
    vote_date: date | None
    motion: str | None
    tallies: dict[str, int]
    records: tuple[VoteRecord, ...]

    @property
    def is_recorded(self) -> bool:
        """An empty panel (heading only) is not a vote record."""
        return bool(self.tallies or self.records)


def tab_candidates(body: bytes, bill_url: str) -> dict[DocumentKind, DocumentCandidate]:
    """Summary and vote tabs present on the bill page, keyed by document kind."""
    soup = BeautifulSoup(body, "html.parser")
    base = bill_url.rstrip("/") + "/"
    found: dict[DocumentKind, DocumentCandidate] = {}
    for tab_tag in soup.select('[role="tab"][data-href], a[href]'):
        href = tab_tag.get("data-href") or tab_tag.get("href")
        if not isinstance(href, str):
            continue
        tab = href.rstrip("/").rsplit("/", 1)[-1]
        if tab in _TABS and not href.startswith("#"):
            kind, location = _TABS[tab]
            found.setdefault(
                kind,
                DocumentCandidate(
                    url=urljoin(base, href), finder_id=FINDER_ID, location=location
                ),
            )
    return found


def _text(tag: Tag | None) -> str:
    return " ".join(tag.get_text(" ", strip=True).split()) if tag else ""


def _date(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%B %d, %Y").date()
    except ValueError:
        return None


def parse_vote_tab(body: bytes) -> list[CommitteeVote]:
    """Every committee vote panel on a Committee Vote tab."""
    soup = BeautifulSoup(body, "html.parser")
    votes = []
    for panel in soup.select("div.committeeVote"):
        heading = panel.select_one(".panel-heading")
        name = _text(heading.select_one("h4") if heading else None)
        question = _text(heading.select_one("p.question") if heading else None)
        motion = re.sub(r"^Question:\s*", "", question) or None
        tallies: dict[str, int] = {}
        records: list[VoteRecord] = []
        for h5 in panel.select(".committeeVoteSummary h5"):
            match = _TALLY.match(_text(h5))
            if match is None:
                continue
            label = match.group(1)
            tallies[label] = int(match.group(2))
            members = h5.find_next_sibling()
            if isinstance(members, Tag) and members.name == "ul":
                records.extend(
                    VoteRecord(member=_text(li), vote=label)
                    for li in members.find_all("li")
                )
        votes.append(
            CommitteeVote(
                committee_id=normalize_committee_name(name) if name else None,
                committee_name=name,
                vote_date=_date(
                    _text(heading.select_one("small") if heading else None)
                ),
                motion=motion,
                tallies=tallies,
                records=tuple(records),
            )
        )
    return votes


VOTE_WORDING = re.compile(r"\bvotes?\b|roll[- ]?call|\bpoll(?:s|ed)?\b", re.I)
_REPORT_HREF = re.compile(r"/Reports/\d+/", re.I)


def report_vote_links(body: bytes, base_url: str) -> list[tuple[str, str]]:
    """(url, link text) of report documents on the bill page that look like votes."""
    soup = BeautifulSoup(body, "html.parser")
    links = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not isinstance(href, str) or not _REPORT_HREF.search(href):
            continue
        text = " ".join(link.get_text(" ", strip=True).split())
        if VOTE_WORDING.search(text) or VOTE_WORDING.search(href):
            links.append((urljoin(base_url, href), text))
    return list(dict.fromkeys(links))


_DOCUMENT_HREF = re.compile(
    r"/Download/DownloadDocument/.*\.(?:pdf|docx)(?:$|\?)", re.I
)


def summary_documents_for(body: bytes, bill_id: str, base_url: str) -> list[str]:
    """Documents on another bill's Committee Summary tab that belong to bill_id.

    A document belongs to the bill when its id appears in the link, its text, or
    its table row.
    """
    soup = BeautifulSoup(body, "html.parser")
    pattern = re.compile(rf"\b{bill_id[0]}\.?\s?{bill_id[1:]}\b", re.I)
    found = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not isinstance(href, str) or not _DOCUMENT_HREF.search(href):
            continue
        row = link.find_parent("tr")
        context = " ".join(
            [
                href,
                link.get_text(" ", strip=True),
                row.get_text(" ", strip=True) if row else "",
            ]
        )
        if pattern.search(context):
            found.append(urljoin(base_url, href))
    return list(dict.fromkeys(found))

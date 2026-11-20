"""Turns a bill page and the documents it leads to into document observations.

Finders, in order: the bill page's tabs; report vote PDFs linked from the bill page;
the Committee Vote tab of a new draft or study order the bill accompanied (except
deadline study orders, which the chair reports without a vote); and documents
attached to the committee's hearings, sorted into summaries and vote records by
title. A search is exhaustive when every finder ran
and none of its fetches failed.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import NAMESPACE_URL, uuid5

from beacon.adapters.documents import read_document
from beacon.domain.captures import Capture
from beacon.domain.observations import (
    ActionType,
    BillHistoryPayload,
    DocumentCandidate,
    DocumentKind,
    DocumentPayload,
    DocumentSearchPayload,
    HearingDocument,
    Observation,
    VoteRecord,
)
from beacon.scraper.documents import bill_tabs
from beacon.scraper.history.committees import get_committee

HEARING_FINDER = "hearing_documents"
REPORT_FINDER = "report_vote_pdfs"
ACCOMPANYING_FINDER = "accompanying_vote_tab"
ACCOMPANYING_SUMMARY_FINDER = "accompanying_summary_tab"
FINDERS = (
    bill_tabs.FINDER_ID,
    REPORT_FINDER,
    ACCOMPANYING_FINDER,
    ACCOMPANYING_SUMMARY_FINDER,
    HEARING_FINDER,
)
_DEADLINE_STUDY = re.compile(
    r"under\s+(?:House\s+Rule\s+27|Joint\s+Rule\s+10|JR\s?10)\b", re.I
)
_SUMMARY_WORDING = re.compile(r"summar", re.I)


def hearing_document_kind(doc: HearingDocument) -> DocumentKind:
    """What a hearing document is, from its title.

    Summary wording wins because summary titles often quote the bill's own title.
    Titles naming only a bill number are summaries.
    """
    if doc.from_content or _SUMMARY_WORDING.search(doc.title):
        return DocumentKind.SUMMARY
    if bill_tabs.VOTE_WORDING.search(doc.title):
        return DocumentKind.VOTES
    return DocumentKind.SUMMARY


def _hearing_documents(
    docs: Sequence[HearingDocument], bill_id: str, kind: DocumentKind
) -> list[HearingDocument]:
    return [
        d for d in docs if bill_id in d.bill_ids and hearing_document_kind(d) == kind
    ]


@dataclass(frozen=True)
class Fetched:
    """A capture and its body."""

    capture: Capture
    body: bytes


@dataclass(frozen=True)
class DocumentPlan:
    """The pages a bill page leads to that the finders need."""

    vote_tab: str | None
    accompanying_tabs: tuple[str, ...]
    accompanying_summary_tabs: tuple[str, ...]
    report_pdfs: tuple[tuple[str, str], ...]

    @property
    def urls(self) -> tuple[str, ...]:
        """Every URL to fetch."""
        tabs = (self.vote_tab,) if self.vote_tab else ()
        return (
            tabs
            + self.accompanying_tabs
            + self.accompanying_summary_tabs
            + tuple(u for u, _ in self.report_pdfs)
        )


def search_extractor_id(kind: DocumentKind, committee_id: str) -> str:
    """Extractor id for one kind of search for one committee."""
    return f"document_search:{kind.value}:{committee_id}"


def document_extractor_id(kind: DocumentKind, committee_id: str) -> str:
    """Extractor id for one kind of document for one committee."""
    return f"document:{kind.value}:{committee_id}"


def plan(
    bill_body: bytes,
    bill_url: str,
    base_url: str,
    session: int,
    history: BillHistoryPayload,
    committee_id: str,
) -> DocumentPlan:
    """Which tabs and documents to fetch for this bill."""
    tabs = bill_tabs.tab_candidates(bill_body, bill_url)
    vote_tab = tabs[DocumentKind.VOTES].url if DocumentKind.VOTES in tabs else None
    accompanying = [
        bill
        for r in history.rows
        if r.action_type == ActionType.ACCOMPANIED
        and r.committee_id in (None, committee_id)
        and not _DEADLINE_STUDY.search(r.raw_text)
        for bill in r.related_bills
    ]
    summary_sources = [
        bill
        for r in history.rows
        if r.action_type == ActionType.ACCOMPANIED
        and r.committee_id in (None, committee_id)
        for bill in r.related_bills
    ]
    return DocumentPlan(
        vote_tab=vote_tab,
        accompanying_summary_tabs=tuple(
            f"{base_url}/Bills/{session}/{b}/CommitteeSummary"
            for b in dict.fromkeys(summary_sources)
        ),
        accompanying_tabs=tuple(
            f"{base_url}/Bills/{session}/{b}/CommitteeVote"
            for b in dict.fromkeys(accompanying)
        ),
        report_pdfs=tuple(bill_tabs.report_vote_links(bill_body, base_url)),
    )


def _observation(
    capture: Capture,
    extractor_id: str,
    session: int,
    bill_id: str,
    committee_id: str,
    payload: DocumentSearchPayload | DocumentPayload,
) -> Observation:
    version = bill_tabs.FINDER_VERSION
    key = f"{capture.capture_id}|{extractor_id}|{version}"
    return Observation(
        observation_id=uuid5(NAMESPACE_URL, key),
        capture_id=capture.capture_id,
        observed_at=capture.requested_at,
        extractor_id=extractor_id,
        extractor_version=version,
        session=session,
        bill_id=bill_id,
        committee_id=committee_id,
        payload=payload,
    )


@dataclass(frozen=True)
class _Found:
    candidate: DocumentCandidate
    created_at: datetime | None = None
    vote_date: date | None = None
    motion: str | None = None
    tallies: Mapping[str, int] | None = None
    records: tuple[VoteRecord, ...] = ()


def _panel(
    fetched: Fetched | None, committee_id: str, finder: str, location: str
) -> _Found | None:
    if fetched is None:
        return None
    votes = bill_tabs.parse_vote_tab(fetched.body)
    mine = next(
        (v for v in votes if v.committee_id == committee_id and v.is_recorded), None
    )
    if mine is None:
        return None
    return _Found(
        DocumentCandidate(url=fetched.capture.url, finder_id=finder, location=location),
        vote_date=mine.vote_date,
        motion=mine.motion,
        tallies=mine.tallies,
        records=mine.records,
    )


def _summaries(
    tabs: Mapping[DocumentKind, DocumentCandidate],
    hearing_documents: Sequence[HearingDocument],
    bill_id: str,
    document_plan: DocumentPlan,
    fetched: Mapping[str, Fetched],
) -> list[_Found]:
    found = [_Found(tabs[DocumentKind.SUMMARY])] if DocumentKind.SUMMARY in tabs else []
    for tab_url in document_plan.accompanying_summary_tabs:
        tab = fetched.get(tab_url)
        if tab is None:
            continue
        source = tab_url.split("/")[-2]
        for url in bill_tabs.summary_documents_for(tab.body, bill_id, tab.capture.url):
            found.append(
                _Found(
                    DocumentCandidate(
                        url=url,
                        finder_id=ACCOMPANYING_SUMMARY_FINDER,
                        location=f"Committee Summary tab of accompanying {source}",
                        confidence=0.95,
                    )
                )
            )
    for doc in _hearing_documents(hearing_documents, bill_id, DocumentKind.SUMMARY):
        found.append(
            _Found(
                DocumentCandidate(
                    url=doc.url,
                    finder_id=HEARING_FINDER,
                    location=f"Hearing document: {doc.title}",
                    confidence=0.85 if doc.from_content else 0.95,
                ),
                created_at=doc.created_at,
            )
        )
    return found


def _votes(
    document_plan: DocumentPlan,
    fetched: Mapping[str, Fetched],
    hearing_documents: Sequence[HearingDocument],
    bill_id: str,
    committee_id: str,
) -> list[_Found]:
    found = []
    if document_plan.vote_tab:
        tab = _panel(
            fetched.get(document_plan.vote_tab),
            committee_id,
            bill_tabs.FINDER_ID,
            "Committee Vote tab",
        )
        found.extend([tab] if tab else [])
    for url in document_plan.accompanying_tabs:
        tab = _panel(
            fetched.get(url),
            committee_id,
            ACCOMPANYING_FINDER,
            f"Committee Vote tab of accompanying {url.split('/')[-2]}",
        )
        found.extend([tab] if tab else [])
    alias = get_committee(committee_id)
    for url, text in document_plan.report_pdfs:
        document = fetched.get(url)
        if document is None or alias is None:
            continue
        content = read_document(document.body)
        if alias.matches(text) or alias.matches(content.text):
            found.append(
                _Found(
                    DocumentCandidate(
                        url=url,
                        finder_id=REPORT_FINDER,
                        location=f"Report: {text}",
                        confidence=0.9,
                    ),
                    created_at=content.created_at,
                )
            )
    for doc in _hearing_documents(hearing_documents, bill_id, DocumentKind.VOTES):
        found.append(
            _Found(
                DocumentCandidate(
                    url=doc.url,
                    finder_id=HEARING_FINDER,
                    location=f"Hearing document: {doc.title}",
                    confidence=0.9,
                ),
                created_at=doc.created_at,
            )
        )
    return found


def _document(kind: DocumentKind, found: list[_Found]) -> DocumentPayload:
    """Combine candidates; a creation date counts only if every candidate has one."""
    created = [f.created_at for f in found]
    known = [c for c in created if c is not None]
    vote = next((f for f in found if f.vote_date), None)
    return DocumentPayload(
        document_kind=kind,
        url=found[0].candidate.url,
        created_at=min(known) if known and len(known) == len(created) else None,
        vote_date=vote.vote_date if vote else None,
        motion=vote.motion if vote else None,
        tallies=dict(vote.tallies or {}) if vote else {},
        records=vote.records if vote else (),
    )


def observe(
    bill_capture: Capture,
    bill_body: bytes,
    document_plan: DocumentPlan,
    fetched: Mapping[str, Fetched],
    hearing_documents: Sequence[HearingDocument] | None,
    session: int,
    bill_id: str,
    committee_id: str,
) -> list[Observation]:
    """A search per document kind for the committee, and a document when found.

    hearing_documents is None when no hearing data existed for the committee at
    the time; the hearing finder then has not run and the search is not exhaustive.
    """
    tabs = bill_tabs.tab_candidates(bill_body, bill_capture.url)
    exhaustive = hearing_documents is not None and all(
        url in fetched for url in document_plan.urls
    )
    consulted = (
        bill_capture.capture_id,
        *tuple(f.capture.capture_id for f in fetched.values()),
    )
    results = {
        DocumentKind.SUMMARY: _summaries(
            tabs, hearing_documents or (), bill_id, document_plan, fetched
        ),
        DocumentKind.VOTES: _votes(
            document_plan, fetched, hearing_documents or (), bill_id, committee_id
        ),
    }
    observations = []
    for kind, found in results.items():
        observations.append(
            _observation(
                bill_capture,
                search_extractor_id(kind, committee_id),
                session,
                bill_id,
                committee_id,
                DocumentSearchPayload(
                    document_kind=kind,
                    candidates=tuple(f.candidate for f in found),
                    finders_tried=FINDERS,
                    exhaustive=exhaustive,
                    consulted=consulted,
                ),
            )
        )
        if found:
            observations.append(
                _observation(
                    bill_capture,
                    document_extractor_id(kind, committee_id),
                    session,
                    bill_id,
                    committee_id,
                    _document(kind, found),
                )
            )
    return observations

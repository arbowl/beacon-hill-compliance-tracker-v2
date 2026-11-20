"""Document finders run together against captured pages."""

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from beacon.domain.captures import Capture
from beacon.domain.observations import (
    CommitteeDocument,
    DocumentKind,
    DocumentPayload,
    DocumentSearchPayload,
    HearingDocument,
)
from beacon.scraper.documents.observe import (
    Fetched,
    hearing_document_kind,
    observe,
    plan,
)
from beacon.scraper.history.extractor import parse_history

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
BASE = "https://malegislature.gov"


def capture(url: str) -> Capture:
    return Capture(
        capture_id=uuid4(),
        run_id=uuid4(),
        url=url,
        requested_at=datetime(2026, 11, 19, tzinfo=UTC),
        http_status=200,
        content_hash="0" * 64,
    )


def fetched(url: str, fixture: str) -> Fetched:
    return Fetched(capture(url), (PAGES / fixture).read_bytes())


def run(
    bill_id: str, sources: dict[str, str], hearing_docs=(), committee_docs=()
) -> dict:
    url = f"{BASE}/Bills/194/{bill_id}"
    body = (PAGES / f"{bill_id}.html").read_bytes()
    document_plan = plan(body, url, BASE, 194, parse_history(body, bill_id), "J19")
    available = {u: fetched(u, f) for u, f in sources.items()}
    observations = observe(
        capture(url),
        body,
        document_plan,
        available,
        hearing_docs,
        committee_docs,
        194,
        bill_id,
        "J19",
    )
    by_kind: dict = {}
    for o in observations:
        if isinstance(o.payload, DocumentSearchPayload | DocumentPayload):
            by_kind.setdefault(o.payload.document_kind, {})[o.payload.kind] = o.payload
    return {"plan": document_plan, "by_kind": by_kind}


def test_vote_on_accompanying_study_order():
    tab = f"{BASE}/Bills/194/S2930/CommitteeVote"
    summary_tab = f"{BASE}/Bills/194/S2930/CommitteeSummary"
    result = run(
        "S1077",
        {tab: "S2930_CommitteeVote.html", summary_tab: "S2930_CommitteeSummary.html"},
    )
    assert result["plan"].accompanying_tabs == (tab,)
    votes = result["by_kind"][DocumentKind.VOTES]
    search = votes["document_search"]
    assert search.exhaustive
    assert [c.finder_id for c in search.candidates] == ["accompanying_vote_tab"]
    assert votes["document"].vote_date == date(2026, 1, 23)


def test_missing_fetch_makes_search_non_exhaustive():
    result = run("S1077", {})
    search = result["by_kind"][DocumentKind.VOTES]["document_search"]
    assert not search.exhaustive
    assert search.candidates == ()


def test_senate_member_vote_report_pdf():
    report = f"{BASE}/Reports/22939/Committee%20-%20Senate%20member%20votes.pdf"
    result = run("S1168", {report: "report_22939.pdf"})
    votes = result["by_kind"][DocumentKind.VOTES]
    assert votes["document_search"].candidates[0].finder_id == "report_vote_pdfs"
    document = votes["document"]
    assert document.created_at is not None
    assert document.created_at.date() == date(2025, 6, 16)


def test_deadline_study_order_is_not_followed():
    result = run("H1648", {})
    assert result["plan"].accompanying_tabs == ()


def test_hearing_document_summary_with_creation_date():
    doc = HearingDocument(
        url=f"{BASE}/Events/DownloadDocument?attachmentId=1",
        title="H67 Bill Summary",
        bill_ids=("H67",),
        created_at=datetime(2025, 4, 1, 9, tzinfo=UTC),
    )
    summary = run("H67", {}, [doc])["by_kind"][DocumentKind.SUMMARY]
    assert summary["document_search"].candidates[0].location == (
        "Hearing document: H67 Bill Summary"
    )
    assert summary["document"].created_at == datetime(2025, 4, 1, 9, tzinfo=UTC)


def test_hearing_vote_document_is_vote_evidence_not_summary():
    doc = HearingDocument(
        url=f"{BASE}/Events/DownloadDocument?attachmentId=3",
        title="H67 - Votes of House Committee Members",
        bill_ids=("H67",),
        created_at=datetime(2025, 5, 2, 14, tzinfo=UTC),
    )
    by_kind = run("H67", {}, [doc])["by_kind"]
    assert by_kind[DocumentKind.SUMMARY]["document_search"].candidates == ()
    votes = by_kind[DocumentKind.VOTES]
    candidate = votes["document_search"].candidates[0]
    assert candidate.finder_id == "hearing_documents"
    assert candidate.location == f"Hearing document: {doc.title}"
    assert votes["document"].created_at == datetime(2025, 5, 2, 14, tzinfo=UTC)
    assert votes["document"].vote_date is None


@pytest.mark.parametrize(
    ("title", "from_content", "kind"),
    [
        ("H67 Bill Summary", False, DocumentKind.SUMMARY),
        ("H67", False, DocumentKind.SUMMARY),
        ("H.67_Summary_Relative to vote by mail", False, DocumentKind.SUMMARY),
        ("H67 - House Committee Members Votes", False, DocumentKind.VOTES),
        ("Roll Call H67", False, DocumentKind.VOTES),
        ("Hearing 4 Vote Packet", True, DocumentKind.SUMMARY),
    ],
)
def test_hearing_document_kind(title, from_content, kind):
    doc = HearingDocument(
        url=f"{BASE}/x", title=title, bill_ids=("H67",), from_content=from_content
    )
    assert hearing_document_kind(doc) == kind


def test_creation_date_dropped_when_another_source_lacks_one():
    doc = HearingDocument(
        url=f"{BASE}/Events/DownloadDocument?attachmentId=2",
        title="H1768 Bill Summary",
        bill_ids=("H1768",),
        created_at=datetime(2025, 4, 1, 9, tzinfo=UTC),
    )
    summary = run("H1768", {}, [doc])["by_kind"][DocumentKind.SUMMARY]
    assert len(summary["document_search"].candidates) == 2
    assert summary["document"].created_at is None


@pytest.mark.parametrize(
    ("hearing_docs", "committee_docs"), [(None, ()), ((), None), (None, None)]
)
def test_uncollected_committee_source_means_not_exhaustive(
    hearing_docs, committee_docs
):
    by_kind = run("H67", {}, hearing_docs, committee_docs)["by_kind"]
    searches = [k["document_search"] for k in by_kind.values()]
    assert len(searches) == 2 and not any(s.exhaustive for s in searches)


def committee_doc(kind: DocumentKind | None, **fields) -> CommitteeDocument:
    return CommitteeDocument(
        url=f"{BASE}/Reports/23967/CPPL%20Poll%20Results%207.7.25.pdf",
        title="CPPL Poll Results 7.7.25",
        posted_on=date(2025, 7, 9),
        document_kind=kind,
        bill_ids=("H67",),
        from_content=True,
        created_at=datetime(2025, 7, 9, 16, 52, tzinfo=UTC),
    ).model_copy(update=fields)


def test_committee_documents_tab_vote_record():
    by_kind = run("H67", {}, committee_docs=[committee_doc(DocumentKind.VOTES)])
    assert by_kind["by_kind"][DocumentKind.SUMMARY]["document_search"].candidates == ()
    votes = by_kind["by_kind"][DocumentKind.VOTES]
    search = votes["document_search"]
    assert search.exhaustive
    assert "committee_documents_tab" in search.finders_tried
    (candidate,) = search.candidates
    assert candidate.finder_id == "committee_documents_tab"
    assert candidate.location == (
        "Committee Documents tab: CPPL Poll Results 7.7.25, posted 2025-07-09"
    )
    assert candidate.confidence == 0.85
    assert votes["document"].created_at == datetime(2025, 7, 9, 16, 52, tzinfo=UTC)


def test_committee_documents_tab_summary_and_unclassified():
    docs = [
        committee_doc(
            DocumentKind.SUMMARY,
            title="H.67 Summary",
            posted_on=None,
            from_content=False,
        ),
        committee_doc(None, title="Public Schedule of Hearings", bill_ids=()),
        committee_doc(DocumentKind.VOTES, bill_ids=("H68",)),
    ]
    by_kind = run("H67", {}, committee_docs=docs)["by_kind"]
    (candidate,) = by_kind[DocumentKind.SUMMARY]["document_search"].candidates
    assert candidate.location == "Committee Documents tab: H.67 Summary"
    assert candidate.confidence == 0.95
    assert by_kind[DocumentKind.VOTES]["document_search"].candidates == ()


def test_summary_on_new_draft_committee_summary_tab():
    tab = f"{BASE}/Bills/194/H5056/CommitteeSummary"
    result = run("H1646", {tab: "H5056_CommitteeSummary.html"})
    assert tab in result["plan"].accompanying_summary_tabs
    candidates = result["by_kind"][DocumentKind.SUMMARY]["document_search"].candidates
    assert [c.finder_id for c in candidates] == ["accompanying_summary_tab"]
    assert "H1646%20Bill%20Summary" in candidates[0].url

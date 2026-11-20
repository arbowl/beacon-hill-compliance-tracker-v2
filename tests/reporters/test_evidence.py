"""Evidence references read back as plain source lines."""

from datetime import UTC, date, datetime
from uuid import uuid4

from beacon.domain.captures import Capture
from beacon.domain.evidence import EvidenceRef
from beacon.domain.observations import (
    ActionType,
    BillHistoryPayload,
    Branch,
    DocumentKind,
    DocumentPayload,
    DocumentSearchPayload,
    HistoryRow,
    Observation,
    Payload,
)
from beacon.reporters.base import EvidenceSource
from beacon.reporters.evidence import describe

URL = "https://malegislature.gov/Bills/194/H1"
AT = datetime(2026, 3, 2, 15, tzinfo=UTC)


def _observation(payload: Payload) -> tuple[Observation, Capture]:
    capture = Capture(
        capture_id=uuid4(),
        run_id=uuid4(),
        url=URL,
        requested_at=AT,
        http_status=200,
        content_hash="0" * 64,
    )
    observation = Observation(
        observation_id=uuid4(),
        capture_id=capture.capture_id,
        observed_at=AT,
        extractor_id="test",
        extractor_version=1,
        session=194,
        bill_id="H1",
        payload=payload,
    )
    return observation, capture


def _search(exhaustive: bool) -> DocumentSearchPayload:
    return DocumentSearchPayload(
        document_kind=DocumentKind.SUMMARY,
        candidates=(),
        finders_tried=("bill_tabs", "hearing_documents"),
        exhaustive=exhaustive,
    )


def test_history_row_is_quoted_with_its_date():
    row = HistoryRow(
        row_index=3,
        action_date=date(2025, 8, 11),
        branch=Branch.HOUSE,
        raw_text="Accompanied a new draft, see H4372",
        action_type=ActionType.ACCOMPANIED,
    )
    observation, capture = _observation(BillHistoryPayload(rows=(row,)))
    item = describe(
        EvidenceRef(observation_id=observation.observation_id, row_index=3),
        observation,
        capture,
    )
    assert item.source == EvidenceSource.HISTORY
    assert (item.occurred_on, item.url) == (date(2025, 8, 11), URL)
    assert item.text == "House: Accompanied a new draft, see H4372"


def test_absence_wording_depends_on_exhaustiveness():
    complete, capture = _observation(_search(exhaustive=True))
    partial, _ = _observation(_search(exhaustive=False))
    ref = EvidenceRef(observation_id=complete.observation_id)
    assert "every source checked" in describe(ref, complete, capture).text
    assert "not proof of absence" in describe(ref, partial, capture).text
    assert describe(ref, complete, capture).occurred_on == date(2026, 3, 2)


def test_undated_document_says_its_date_is_retrieval():
    payload = DocumentPayload(document_kind=DocumentKind.VOTES, url="https://x/doc.pdf")
    observation, capture = _observation(payload)
    item = describe(
        EvidenceRef(observation_id=observation.observation_id), observation, capture
    )
    assert item.url == "https://x/doc.pdf"
    assert "undated" in item.text

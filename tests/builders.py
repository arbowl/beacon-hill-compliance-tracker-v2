"""Concise constructors for test observations."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from beacon.domain.observations import (
    ActionType,
    BillHistoryPayload,
    Branch,
    DocumentCandidate,
    DocumentKind,
    DocumentPayload,
    DocumentSearchPayload,
    HearingPayload,
    HistoryRow,
    Observation,
)

ET = ZoneInfo("America/New_York")


def row(
    action_date: str,
    action_type: ActionType,
    committee_id: str | None = None,
    *,
    branch: Branch = Branch.HOUSE,
    text: str = "",
    hearing_date: str | None = None,
    new_deadline: str | None = None,
    related_bills: tuple[str, ...] = (),
) -> dict:
    """A history row spec; row_index is assigned by history()."""
    return {
        "action_date": date.fromisoformat(action_date),
        "action_type": action_type,
        "committee_id": committee_id,
        "branch": branch,
        "raw_text": text or action_type.value,
        "hearing_date": date.fromisoformat(hearing_date) if hearing_date else None,
        "new_deadline": date.fromisoformat(new_deadline) if new_deadline else None,
        "related_bills": related_bills,
    }


def history(
    bill_id: str,
    rows: Sequence[dict | list[dict]],
    *,
    session: int = 194,
    observed_at: datetime | None = None,
    capture_id: UUID | None = None,
    extractor_version: int = 1,
) -> Observation:
    """A bill history observation; a list of specs shares one row_index."""
    return Observation(
        observation_id=uuid4(),
        capture_id=capture_id or uuid4(),
        observed_at=observed_at or datetime(2026, 1, 1, tzinfo=UTC),
        extractor_id="bill_history",
        extractor_version=extractor_version,
        session=session,
        bill_id=bill_id,
        payload=BillHistoryPayload(
            rows=tuple(
                HistoryRow(row_index=i, **spec)
                for i, group in enumerate(rows)
                for spec in (group if isinstance(group, list) else [group])
            )
        ),
    )


def search(
    bill_id: str,
    document_kind: DocumentKind,
    observed_at: str,
    *,
    committee_id: str = "J19",
    found: bool = False,
    exhaustive: bool = True,
) -> Observation:
    """A document search observation at an ISO datetime (ET if naive)."""
    candidates = (
        (
            DocumentCandidate(
                url=f"https://x/{bill_id}/{document_kind.value}",
                finder_id="test",
                location="test",
            ),
        )
        if found
        else ()
    )
    return Observation(
        observation_id=uuid4(),
        capture_id=uuid4(),
        observed_at=_moment(observed_at),
        extractor_id=f"document_search:{document_kind.value}",
        extractor_version=1,
        session=194,
        bill_id=bill_id,
        committee_id=committee_id,
        payload=DocumentSearchPayload(
            document_kind=document_kind,
            candidates=candidates,
            finders_tried=("test",),
            exhaustive=exhaustive,
        ),
    )


def document(
    bill_id: str,
    document_kind: DocumentKind,
    observed_at: str,
    *,
    committee_id: str = "J19",
    created_at: str | None = None,
    vote_date: str | None = None,
) -> Observation:
    """A captured document observation."""
    return Observation(
        observation_id=uuid4(),
        capture_id=uuid4(),
        observed_at=_moment(observed_at),
        extractor_id=f"document:{document_kind.value}",
        extractor_version=1,
        session=194,
        bill_id=bill_id,
        committee_id=committee_id,
        payload=DocumentPayload(
            document_kind=document_kind,
            url=f"https://x/{bill_id}/{document_kind.value}",
            created_at=_moment(created_at) if created_at else None,
            vote_date=date.fromisoformat(vote_date) if vote_date else None,
        ),
    )


def _moment(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=ET)


def hearing(
    hearing_id: str,
    hearing_date: str,
    docket: tuple[str, ...],
    *,
    committee_id: str = "J19",
    observed_at: str = "2026-01-01T02:00",
) -> Observation:
    """A hearing page observation."""
    return Observation(
        observation_id=uuid4(),
        capture_id=uuid4(),
        observed_at=_moment(observed_at),
        extractor_id="hearing",
        extractor_version=2,
        session=194,
        committee_id=committee_id,
        payload=HearingPayload(
            hearing_id=hearing_id,
            hearing_date=date.fromisoformat(hearing_date),
            docket=docket,
            documents=(),
        ),
    )

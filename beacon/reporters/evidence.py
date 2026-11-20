"""Turns evidence references into plain, dated lines of source text."""

from beacon.domain.captures import Capture
from beacon.domain.evidence import EvidenceRef
from beacon.domain.local_time import local_date
from beacon.domain.observations import (
    BillHistoryPayload,
    CommitteeBillListPayload,
    CommitteeDocumentsPayload,
    DocumentPayload,
    DocumentSearchPayload,
    HearingPayload,
    Observation,
)
from beacon.reporters.base import EvidenceItem, EvidenceSource

_KIND_TEXT = {"summary": "summary", "votes": "committee vote record"}


def describe(
    ref: EvidenceRef, observation: Observation, capture: Capture
) -> EvidenceItem:
    """What the referenced observation says, linked to the page it was read from."""
    payload = observation.payload
    seen_on = local_date(observation.observed_at)
    match payload:
        case BillHistoryPayload():
            rows = [r for r in payload.rows if r.row_index == ref.row_index]
            if not rows:
                return EvidenceItem(
                    EvidenceSource.HISTORY,
                    seen_on,
                    "Bill history as captured",
                    capture.url,
                )
            row = rows[0]
            return EvidenceItem(
                EvidenceSource.HISTORY,
                row.action_date,
                f"{row.branch.value}: {row.raw_text}",
                capture.url,
            )
        case DocumentSearchPayload():
            kind = _KIND_TEXT[payload.document_kind.value]
            if payload.candidates:
                places = "; ".join(
                    dict.fromkeys(c.location for c in payload.candidates)
                )
                return EvidenceItem(
                    EvidenceSource.SEARCH,
                    seen_on,
                    f"Found the {kind}: {places}",
                    payload.candidates[0].url,
                )
            scope = (
                "every source checked"
                if payload.exhaustive
                else "search incomplete, not proof of absence"
            )
            return EvidenceItem(
                EvidenceSource.SEARCH,
                seen_on,
                f"No {kind} found ({scope}; looked in "
                f"{', '.join(payload.finders_tried)})",
                capture.url,
            )
        case DocumentPayload():
            kind = _KIND_TEXT[payload.document_kind.value]
            parts = [f"The {kind}"]
            if payload.vote_date:
                parts.append(f"records a vote on {payload.vote_date.isoformat()}")
            if payload.created_at:
                created = local_date(payload.created_at).isoformat()
                parts.append(f"file created {created}")
            if len(parts) == 1:
                parts.append("undated; the date shown is when it was retrieved")
            return EvidenceItem(
                EvidenceSource.DOCUMENT,
                payload.vote_date
                or (local_date(payload.created_at) if payload.created_at else seen_on),
                ", ".join(parts),
                payload.url,
            )
        case HearingPayload():
            held = (
                f"held {payload.hearing_date.isoformat()}"
                if payload.hearing_date
                else "date not listed"
            )
            return EvidenceItem(
                EvidenceSource.HEARING,
                payload.hearing_date or seen_on,
                f"Hearing {payload.hearing_id}, {held}; {len(payload.docket)} bills on "
                f"the docket, {len(payload.documents)} documents attached",
                capture.url,
            )
        case CommitteeDocumentsPayload():
            covered = sum(1 for d in payload.documents if d.bill_ids)
            return EvidenceItem(
                EvidenceSource.COMMITTEE_DOCUMENTS,
                seen_on,
                f"Committee Documents tab: {len(payload.documents)} documents, "
                f"{covered} naming bills",
                capture.url,
            )
        case CommitteeBillListPayload():
            return EvidenceItem(
                EvidenceSource.LISTING,
                seen_on,
                f"Committee bill list ({len(payload.bill_ids)} bills)",
                capture.url,
            )

"""Append-only facts about what a captured page said."""

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ObservationKind(StrEnum):
    """The kind of page an observation was extracted from."""

    COMMITTEE_BILL_LIST = "committee_bill_list"
    BILL_HISTORY = "bill_history"
    DOCUMENT_SEARCH = "document_search"
    DOCUMENT = "document"
    HEARING = "hearing"


class DocumentKind(StrEnum):
    """Documents whose posting the rules require."""

    SUMMARY = "summary"
    VOTES = "votes"


class Branch(StrEnum):
    """The branch column of a bill history row."""

    HOUSE = "House"
    SENATE = "Senate"
    JOINT = "Joint"
    EXECUTIVE = "Executive"
    OTHER = "Other"

    @staticmethod
    def parse(text: str) -> "Branch":
        """Map the site's branch text to a Branch."""
        cleaned = text.strip().lower()
        for branch in (Branch.HOUSE, Branch.SENATE, Branch.JOINT):
            if branch.value.lower() in cleaned:
                return branch
        if "executive" in cleaned or "governor" in cleaned:
            return Branch.EXECUTIVE
        return Branch.OTHER


class ActionType(StrEnum):
    """Classification of a bill history row."""

    REFERRED = "REFERRED"
    DISCHARGED = "DISCHARGED"
    REPORTED = "REPORTED"
    STUDY_ORDER = "STUDY_ORDER"
    ACCOMPANIED = "ACCOMPANIED"
    HEARING_SCHEDULED = "HEARING_SCHEDULED"
    HEARING_RESCHEDULED = "HEARING_RESCHEDULED"
    HEARING_LOCATION_CHANGED = "HEARING_LOCATION_CHANGED"
    HEARING_TIME_CHANGED = "HEARING_TIME_CHANGED"
    REPORTING_EXTENDED = "REPORTING_EXTENDED"
    READ = "READ"
    READ_SECOND = "READ_SECOND"
    READ_THIRD = "READ_THIRD"
    RULES_SUSPENDED = "RULES_SUSPENDED"
    CONCURRED = "CONCURRED"
    PASSED_TO_BE_ENGROSSED = "PASSED_TO_BE_ENGROSSED"
    ENACTED = "ENACTED"
    SIGNED = "SIGNED"
    PLACED_IN_ORDERS = "PLACED_IN_ORDERS"
    REFERRED_TO_BILLS_IN_THIRD_READING = "REFERRED_TO_BILLS_IN_THIRD_READING"
    STEERING_REFERRAL = "STEERING_REFERRAL"
    AMENDED = "AMENDED"
    TITLE_CHANGED = "TITLE_CHANGED"
    EMERGENCY_PREAMBLE = "EMERGENCY_PREAMBLE"
    UNKNOWN = "UNKNOWN"


class HistoryRow(BaseModel):
    """One classified action from a bill's history table.

    A table row describing several actions yields several HistoryRows sharing a
    row_index.
    """

    model_config = ConfigDict(frozen=True)

    row_index: int
    action_date: date
    branch: Branch
    raw_text: str
    action_type: ActionType
    committee_id: str | None = None
    hearing_date: date | None = None
    new_deadline: date | None = None
    related_bills: tuple[str, ...] = ()


class BillHistoryPayload(BaseModel):
    """A snapshot of a bill's history table."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[ObservationKind.BILL_HISTORY] = ObservationKind.BILL_HISTORY
    title: str | None = None
    rows: tuple[HistoryRow, ...]


class CommitteeBillListPayload(BaseModel):
    """A snapshot of the bills listed on a committee's bills tab."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[ObservationKind.COMMITTEE_BILL_LIST] = (
        ObservationKind.COMMITTEE_BILL_LIST
    )
    bill_ids: tuple[str, ...]


class DocumentCandidate(BaseModel):
    """A document a finder located."""

    model_config = ConfigDict(frozen=True)

    url: str
    finder_id: str
    location: str
    confidence: float | None = None


class DocumentSearchPayload(BaseModel):
    """The result of searching for one kind of document for one committee.

    An exhaustive search ran every finder; only an exhaustive search with no
    candidates is evidence of absence. The observation's capture is the bill page;
    consulted lists every capture the search read.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal[ObservationKind.DOCUMENT_SEARCH] = ObservationKind.DOCUMENT_SEARCH
    document_kind: DocumentKind
    candidates: tuple[DocumentCandidate, ...]
    finders_tried: tuple[str, ...]
    exhaustive: bool
    consulted: tuple[UUID, ...] = ()


class VoteRecord(BaseModel):
    """One member's recorded vote."""

    model_config = ConfigDict(frozen=True)

    member: str
    vote: str


class DocumentPayload(BaseModel):
    """What a captured summary or vote document says."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[ObservationKind.DOCUMENT] = ObservationKind.DOCUMENT
    document_kind: DocumentKind
    url: str
    created_at: AwareDatetime | None = None
    vote_date: date | None = None
    motion: str | None = None
    tallies: dict[str, int] = {}
    records: tuple[VoteRecord, ...] = ()
    text_length: int = 0


class HearingDocument(BaseModel):
    """A document attached to a hearing and the bills it covers.

    bill_ids come from the title, or for multi-bill packets from per-bill headers in
    the text, restricted to the hearing's docket.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    title: str
    bill_ids: tuple[str, ...]
    from_content: bool = False
    created_at: AwareDatetime | None = None


class HearingPayload(BaseModel):
    """A hearing detail page: date, docket, and attached documents."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[ObservationKind.HEARING] = ObservationKind.HEARING
    hearing_id: str
    hearing_date: date | None
    docket: tuple[str, ...]
    documents: tuple[HearingDocument, ...]
    consulted: tuple[UUID, ...] = ()


Payload = Annotated[
    BillHistoryPayload
    | CommitteeBillListPayload
    | DocumentSearchPayload
    | DocumentPayload
    | HearingPayload,
    Field(discriminator="kind"),
]


class Observation(BaseModel):
    """What one extractor version read from one capture."""

    model_config = ConfigDict(frozen=True)

    observation_id: UUID
    capture_id: UUID
    observed_at: AwareDatetime
    extractor_id: str
    extractor_version: int
    session: int
    bill_id: str | None = None
    committee_id: str | None = None
    payload: Payload

    @property
    def kind(self) -> ObservationKind:
        """The payload kind."""
        return self.payload.kind

"""Projects observations into rule-free facts about a bill."""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from uuid import UUID

from beacon.domain.evidence import EvidenceRef
from beacon.domain.ids import BillKey
from beacon.domain.local_time import local_date
from beacon.domain.observations import (
    ActionType,
    BillHistoryPayload,
    Branch,
    DocumentKind,
    DocumentPayload,
    DocumentSearchPayload,
    HearingPayload,
    HistoryRow,
    Observation,
    PublicBasis,
)
from beacon.evaluator.record_gaps import (
    RECORD_GAPS,
    RecordGap,
    SuppliedReferral,
    UnrecordedTransfer,
    gaps_for,
)

TERMINAL_ACTIONS = frozenset(
    {ActionType.REPORTED, ActionType.STUDY_ORDER, ActionType.ACCOMPANIED}
)
REFERRAL_ACTIONS = frozenset(
    {
        ActionType.REFERRED,
        ActionType.DISCHARGED,
        ActionType.REFERRED_TO_BILLS_IN_THIRD_READING,
    }
)
HEARING_ACTIONS = frozenset(
    {
        ActionType.HEARING_SCHEDULED,
        ActionType.HEARING_RESCHEDULED,
        ActionType.HEARING_LOCATION_CHANGED,
        ActionType.HEARING_TIME_CHANGED,
    }
)


@dataclass(frozen=True)
class HearingNotice:
    """A hearing announcement or amendment, or a hearing docket listing the bill.

    announced_on is None for docket listings, which carry no announcement date.
    """

    announced_on: date | None
    action_type: ActionType
    hearing_date: date | None
    evidence: EvidenceRef


@dataclass(frozen=True)
class Hearing:
    """One hearing of a tenure, as its announcements resolve.

    notice is the announcement that set hearing_date, or None for a hearing known
    only from a docket listing. notices holds every announcement attributed to the
    hearing in posting order: the notice, restatements of its date, time and
    place updates, and announcements of dates it was moved from.
    """

    hearing_date: date
    notice: HearingNotice | None
    notices: tuple[HearingNotice, ...]


@dataclass
class _Draft:
    hearing_date: date
    notice: HearingNotice | None
    notices: list[HearingNotice]


def _schedule(notices: Iterable[HearingNotice]) -> tuple[Hearing, ...]:
    """Resolve announcements in posting order into hearings.

    A scheduled or rescheduled date matching a hearing already set restates it. A
    reschedule to a new date moves the most recently set hearing still pending when
    it is posted, and the reschedule becomes that hearing's notice; with none
    pending it sets a new hearing. Time and place updates attach to the hearing
    they precede, else to the latest hearing.
    """
    drafts: list[_Draft] = []
    for n in notices:
        if n.hearing_date is None:
            pending = [
                d
                for d in drafts
                if n.announced_on is None or d.hearing_date >= n.announced_on
            ]
            target = (pending or drafts or [None])[-1]
            if target is not None:
                target.notices.append(n)
            continue
        same = next((d for d in drafts if d.hearing_date == n.hearing_date), None)
        if same is not None:
            same.notices.append(n)
            if same.notice is None and n.announced_on is not None:
                same.notice = n
            continue
        pending = (
            [d for d in drafts if d.hearing_date > n.announced_on]
            if n.action_type == ActionType.HEARING_RESCHEDULED
            and n.announced_on is not None
            else []
        )
        if pending:
            moved = pending[-1]
            moved.hearing_date, moved.notice = n.hearing_date, n
            moved.notices.append(n)
            continue
        drafts.append(
            _Draft(n.hearing_date, n if n.announced_on is not None else None, [n])
        )
    return tuple(
        sorted(
            (Hearing(d.hearing_date, d.notice, tuple(d.notices)) for d in drafts),
            key=lambda h: h.hearing_date,
        )
    )


@dataclass(frozen=True)
class Extension:
    """A recorded extension of a reporting deadline."""

    granted_on: date
    until: date | None
    branch: Branch
    raw_text: str
    evidence: EvidenceRef


@dataclass(frozen=True)
class Report:
    """A terminal committee action."""

    reported_on: date
    action_type: ActionType
    raw_text: str
    evidence: EvidenceRef


@dataclass(frozen=True)
class Tenure:
    """One referral of a bill to a committee, until report or discharge."""

    tenure_id: str
    committee_id: str
    referred_on: date
    referral: EvidenceRef
    hearing_notices: tuple[HearingNotice, ...]
    extensions: tuple[Extension, ...]
    report: Report | None
    discharged_on: date | None
    discharged_to: str | None

    def hearings(self) -> tuple[Hearing, ...]:
        """The tenure's hearings in date order."""
        return _schedule(self.hearing_notices)

    def hearing_dates(self) -> tuple[date, ...]:
        """Distinct hearing dates in order."""
        return tuple(h.hearing_date for h in self.hearings())


@dataclass(frozen=True)
class Origin:
    """A document produced by a committee (new draft, study order), not referred."""

    committee_id: str | None
    originated_on: date
    source_bills: tuple[str, ...]
    evidence: EvidenceRef


@dataclass(frozen=True)
class Held:
    """A committee that held a vehicle during a ride, from the day the rider came
    before it: the later of boarding and the vehicle's referral there."""

    committee_id: str
    from_on: date


@dataclass(frozen=True)
class Ride:
    """The bill accompanying another, the vehicle, from accompanied_on until its
    own next referral (released_on, None while it rides), and the committees that
    held the vehicle meanwhile. A committee lists the riders with the vehicle."""

    vehicle: str
    accompanied_on: date
    released_on: date | None
    held: tuple[Held, ...]
    evidence: EvidenceRef

    @property
    def committee_ids(self) -> tuple[str, ...]:
        return tuple(h.committee_id for h in self.held)

    def held_by(self, committee_id: str) -> Held | None:
        return next((h for h in self.held if h.committee_id == committee_id), None)


@dataclass(frozen=True)
class DocumentEvidence:
    """What searches show about one kind of document for one committee.

    absent_before is the latest exhaustive search that found nothing, before the
    document was first seen (or ever, if it never was). public_from is the earliest
    moment it could have been public, and public_basis what shows that.
    """

    document_kind: DocumentKind
    committee_id: str | None
    first_seen_at: datetime | None
    absent_before: datetime | None
    created_at: datetime | None
    public_from: datetime | None
    public_basis: PublicBasis | None
    vote_date: date | None
    url: str | None
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class BillFacts:
    """Everything known about a bill, derived from its latest observations."""

    bill: BillKey
    tenures: tuple[Tenure, ...]
    last_observed_at: datetime | None
    source_observations: tuple[UUID, ...]
    origin: Origin | None = None
    documents: tuple[DocumentEvidence, ...] = ()
    rides: tuple[Ride, ...] = ()
    unrecorded: tuple[UnrecordedTransfer, ...] = ()
    stale_gaps: tuple[RecordGap, ...] = ()

    def document(
        self, document_kind: DocumentKind, committee_id: str | None
    ) -> DocumentEvidence | None:
        """Evidence for one kind of document, or None if never searched."""
        return next(
            (
                d
                for d in self.documents
                if d.document_kind == document_kind and d.committee_id == committee_id
            ),
            None,
        )

    def tenures_for(self, committee_id: str) -> tuple[Tenure, ...]:
        """Tenures before one committee, in order."""
        return tuple(t for t in self.tenures if t.committee_id == committee_id)

    def carried_before(self, committee_id: str) -> Ride | None:
        """The first ride on a vehicle that the committee held, if any."""
        return next((r for r in self.rides if committee_id in r.committee_ids), None)

    def unrecorded_before(self, committee_id: str) -> UnrecordedTransfer | None:
        """A transfer to the committee that the record omits, if one is known."""
        return next(
            (g for g in self.unrecorded if g.committee_id == committee_id), None
        )


@dataclass
class _TenureBuilder:
    committee_id: str
    referred_on: date
    referral: EvidenceRef
    notices: list[HearingNotice] = field(default_factory=list)
    extensions: list[Extension] = field(default_factory=list)
    report: Report | None = None
    discharged_on: date | None = None
    discharged_to: str | None = None

    @property
    def is_open(self) -> bool:
        return self.report is None and self.discharged_on is None

    def build(self, bill: BillKey) -> Tenure:
        return Tenure(
            tenure_id=f"{bill}:{self.committee_id}:{self.referred_on.isoformat()}",
            committee_id=self.committee_id,
            referred_on=self.referred_on,
            referral=self.referral,
            hearing_notices=tuple(self.notices),
            extensions=tuple(self.extensions),
            report=self.report,
            discharged_on=self.discharged_on,
            discharged_to=self.discharged_to,
        )


def _current_history(
    observations: Iterable[Observation],
) -> Observation | None:
    """Latest capture's observation, using the highest extractor version."""
    best_per_capture: dict[UUID, Observation] = {}
    for obs in observations:
        if not isinstance(obs.payload, BillHistoryPayload):
            continue
        held = best_per_capture.get(obs.capture_id)
        if held is None or obs.extractor_version > held.extractor_version:
            best_per_capture[obs.capture_id] = obs
    if not best_per_capture:
        return None
    return max(best_per_capture.values(), key=lambda o: o.observed_at)


def _open(
    builders: list[_TenureBuilder], committee_id: str | None
) -> _TenureBuilder | None:
    for b in reversed(builders):
        if b.committee_id == committee_id and b.is_open:
            return b
    return None


def _open_on(builders: list[_TenureBuilder], on: date) -> _TenureBuilder | None:
    """The latest tenure that was open on a date."""
    for b in reversed(builders):
        end = (b.report.reported_on if b.report else None) or b.discharged_on
        if b.referred_on <= on and (end is None or on <= end):
            return b
    return None


def _without_repeats(rows: Iterable[HistoryRow]) -> list[HistoryRow]:
    """Drop a table row that repeats the one before it action for action.

    Histories sometimes list one entry twice; replayed, a repeated "reported and
    referred" would report out of the committee the first copy referred to.
    """
    groups: dict[int, list[HistoryRow]] = {}
    for r in rows:
        groups.setdefault(r.row_index, []).append(r)
    kept: list[HistoryRow] = []
    previous: list[dict] | None = None
    for _, group in sorted(groups.items()):
        actions = [r.model_dump(exclude={"row_index"}) for r in group]
        if actions != previous:
            kept.extend(group)
        previous = actions
    return kept


def _ordered_rows(obs: Observation) -> list[HistoryRow]:
    """Rows by date and table order; within a row, terminal actions, then
    referrals, then the rest. That way "ordered to a third reading, read third"
    opens the tenure the reading closes. A row repeating the one before it is
    dropped."""
    assert isinstance(obs.payload, BillHistoryPayload)
    return sorted(
        _without_repeats(obs.payload.rows),
        key=lambda r: (
            r.action_date,
            r.row_index,
            r.action_type not in TERMINAL_ACTIONS,
            r.action_type not in REFERRAL_ACTIONS,
        ),
    )


def _origin(obs: Observation, rows: list[HistoryRow]) -> Origin | None:
    """A report preceding any referral marks a committee-originated document."""
    first = next(
        (r for r in rows if r.action_type in TERMINAL_ACTIONS | REFERRAL_ACTIONS),
        None,
    )
    if first is None or first.action_type not in TERMINAL_ACTIONS:
        return None
    same_day = [r for r in rows if r.action_date == first.action_date]
    sources = tuple(dict.fromkeys(b for r in same_day for b in r.related_bills))
    return Origin(
        committee_id=first.committee_id,
        originated_on=first.action_date,
        source_bills=sources,
        evidence=EvidenceRef(
            observation_id=obs.observation_id, row_index=first.row_index
        ),
    )


_VEHICLE_SIDE = re.compile(r"\baccompanied\s+by\b", re.I)


def _boarded(rows: list[HistoryRow]) -> list[tuple[int, HistoryRow, str]]:
    """Each (position, row, vehicle) where the bill accompanied another.

    "Accompanied by X" is the vehicle's side and names its riders; "Accompanied
    X" and "Accompanied a study order, see X" are the rider's.
    """
    return [
        (i, r, vehicle)
        for i, r in enumerate(rows)
        if r.action_type == ActionType.ACCOMPANIED
        and not _VEHICLE_SIDE.search(r.raw_text)
        for vehicle in r.related_bills
    ]


def _rides(
    bill: BillKey,
    obs: Observation,
    rows: list[HistoryRow],
    vehicle_history: Callable[[str], Iterable[Observation]],
    cutoff: date | None,
) -> tuple[tuple[Ride, ...], list[UUID]]:
    """The bill's rides, with the vehicle observations they rest on."""
    rides, used = [], []
    for i, boarded, vehicle in _boarded(rows):
        released_on = next(
            (
                r.action_date
                for r in rows[i + 1 :]
                if r.action_type in REFERRAL_ACTIONS and r.committee_id
            ),
            None,
        )
        current = _current_history(vehicle_history(vehicle))
        held: dict[str, Held] = {}
        if current is not None:
            vehicle_rows = [
                r
                for r in _ordered_rows(current)
                if cutoff is None or r.action_date <= cutoff
            ]
            key = BillKey(bill.session, vehicle)
            for t in _tenures(key, current, vehicle_rows):
                if (released_on is None or t.referred_on <= released_on) and (
                    _end(t) is None or boarded.action_date <= _end(t)
                ):
                    from_on = max(t.referred_on, boarded.action_date)
                    held.setdefault(t.committee_id, Held(t.committee_id, from_on))
            used.append(current.observation_id)
        rides.append(
            Ride(
                vehicle=vehicle,
                accompanied_on=boarded.action_date,
                released_on=released_on,
                held=tuple(held.values()),
                evidence=EvidenceRef(
                    observation_id=obs.observation_id, row_index=boarded.row_index
                ),
            )
        )
    return tuple(rides), used


_NEW_DRAFT = re.compile(r"^(?:Recommended\s+)?new\s+draft\b", re.I)
_SUBSTITUTED = re.compile(r"\bnew\s+draft\b.*\bsubstituted\b", re.I)


def _substitutions(
    obs: Observation,
    rows: list[HistoryRow],
    vehicle_history: Callable[[str], Iterable[Observation]],
    cutoff: date | None,
) -> tuple[tuple[Ride, ...], list[UUID]]:
    """Rides for a new draft substituted for the bill a committee held.

    A draft substituted on the floor ("New draft (X) of S1", while S1 reads "New
    draft (X) substituted, see S2") starts where its predecessor's tenure ended:
    the committee lists the new draft, but held only its predecessor, which is
    judged instead. A link needs the predecessor's own row naming the draft, on
    the day one of its tenures ended.
    """
    rides, used = [], []
    for r in rows:
        if not _NEW_DRAFT.search(r.raw_text):
            continue
        for parent in r.related_bills:
            current = _current_history(vehicle_history(parent))
            if current is None:
                continue
            parent_rows = [
                p
                for p in _ordered_rows(current)
                if cutoff is None or p.action_date <= cutoff
            ]
            named = [
                p
                for p in parent_rows
                if _SUBSTITUTED.search(p.raw_text) and obs.bill_id in p.related_bills
            ]
            if not named:
                continue
            substituted_on = named[0].action_date
            key = BillKey(obs.session, parent)
            held = {
                t.committee_id: Held(t.committee_id, substituted_on)
                for t in _tenures(key, current, parent_rows)
                if _end(t) == substituted_on
            }
            if not held:
                continue
            rides.append(
                Ride(
                    vehicle=parent,
                    accompanied_on=substituted_on,
                    released_on=None,
                    held=tuple(held.values()),
                    evidence=EvidenceRef(
                        observation_id=obs.observation_id, row_index=r.row_index
                    ),
                )
            )
            used.append(current.observation_id)
    return tuple(rides), used


def _supply_referrals(
    rows: list[HistoryRow], gaps: Iterable[SuppliedReferral]
) -> tuple[list[HistoryRow], list[SuppliedReferral]]:
    """Insert each referral the history omits just before its anchor row.

    The supplied row is dated referred_on but placed after every row of that day;
    nothing else the bill did that day can close it, and it shares the anchor's
    row_index, which is the evidence cited for it. A gap whose committee the
    history already refers the bill to, or whose anchor row is missing, is stale.
    """
    stale = []
    for gap in gaps:
        referred = any(
            r.action_type in REFERRAL_ACTIONS and r.committee_id == gap.committee_id
            for r in rows
        )
        at = next(
            (
                i
                for i, r in enumerate(rows)
                if r.action_date == gap.anchor_on
                and r.raw_text.startswith(gap.anchor_prefix)
            ),
            None,
        )
        if referred or at is None:
            stale.append(gap)
            continue
        anchor = rows[at]
        while at > 0 and rows[at - 1].row_index == anchor.row_index:
            at -= 1
        supplied = anchor.model_copy(
            update={
                "action_date": gap.referred_on,
                "action_type": ActionType.REFERRED,
                "committee_id": gap.committee_id,
                "hearing_date": None,
                "new_deadline": None,
                "related_bills": (),
            }
        )
        rows = [*rows[:at], supplied, *rows[at:]]
    return rows, stale


def _end(tenure: Tenure) -> date | None:
    return (tenure.report.reported_on if tenure.report else None) or (
        tenure.discharged_on
    )


def _tenures(
    bill: BillKey, obs: Observation, rows: list[HistoryRow]
) -> tuple[Tenure, ...]:
    builders: list[_TenureBuilder] = []
    current: str | None = None
    # A history row can split into a dated hearing action and an undated time or
    # place change; both follow the date when routing to a tenure.
    held_on = {
        r.row_index: r.hearing_date
        for r in rows
        if r.action_type in HEARING_ACTIONS and r.hearing_date is not None
    }
    for r in rows:
        ref = EvidenceRef(observation_id=obs.observation_id, row_index=r.row_index)
        if r.action_type in REFERRAL_ACTIONS and r.committee_id:
            if (
                current
                and current != r.committee_id
                and (prior := _open(builders, current))
            ):
                prior.discharged_on = r.action_date
                prior.discharged_to = r.committee_id
            if _open(builders, r.committee_id) is None:
                builders.append(_TenureBuilder(r.committee_id, r.action_date, ref))
            current = r.committee_id
        elif (tenure := _open(builders, r.committee_id or current)) is None:
            continue
        elif r.action_type in HEARING_ACTIONS:
            held = held_on.get(r.row_index)
            if (
                held is not None
                and held < tenure.referred_on
                and (tenure := _open_on(builders, held)) is None
            ):
                continue
            tenure.notices.append(
                HearingNotice(r.action_date, r.action_type, r.hearing_date, ref)
            )
        elif r.action_type == ActionType.REPORTING_EXTENDED:
            tenure.extensions.append(
                Extension(r.action_date, r.new_deadline, r.branch, r.raw_text, ref)
            )
        elif r.action_type in TERMINAL_ACTIONS or (
            # Read third: out of Bills in the Third Reading, the only committee a
            # history attributes the reading to.
            r.action_type == ActionType.READ_THIRD and r.committee_id is not None
        ):
            tenure.report = Report(r.action_date, r.action_type, r.raw_text, ref)
    return tuple(b.build(bill) for b in builders)


def _document_evidence(
    key: tuple[DocumentKind, str | None], observations: list[Observation]
) -> DocumentEvidence:
    searches = sorted(
        (
            (o.observed_at, o.payload, o)
            for o in observations
            if isinstance(o.payload, DocumentSearchPayload)
        ),
        key=lambda t: t[0],
    )
    found = [o for _, payload, o in searches if payload.candidates]
    first_seen = found[0].observed_at if found else None
    absent = [
        at
        for at, payload, _ in searches
        if not payload.candidates
        and payload.exhaustive
        and (first_seen is None or at < first_seen)
    ]
    documents = sorted(
        (
            (o.observed_at, o.payload, o)
            for o in observations
            if isinstance(o.payload, DocumentPayload)
        ),
        key=lambda t: t[0],
    )
    latest = documents[-1][1] if documents else None
    public = latest.public_bound if latest else None
    evidence_obs = ([found[0]] if found else []) + [t[2] for t in documents[-1:]]
    return DocumentEvidence(
        document_kind=key[0],
        committee_id=key[1],
        first_seen_at=first_seen,
        absent_before=max(absent, default=None),
        created_at=latest.created_at if latest else None,
        public_from=public[0] if public else None,
        public_basis=public[1] if public else None,
        vote_date=latest.vote_date if latest else None,
        url=latest.url if latest else None,
        evidence=tuple(
            EvidenceRef(observation_id=o.observation_id) for o in evidence_obs
        ),
    )


def _documents(observations: list[Observation]) -> tuple[DocumentEvidence, ...]:
    """Group document observations, keeping the newest version per capture."""
    newest: dict[tuple[UUID, str], Observation] = {}
    for o in observations:
        if isinstance(o.payload, DocumentSearchPayload | DocumentPayload):
            held = newest.get((o.capture_id, o.extractor_id))
            if held is None or o.extractor_version > held.extractor_version:
                newest[(o.capture_id, o.extractor_id)] = o
    groups: dict[tuple[DocumentKind, str | None], list[Observation]] = {}
    for o in newest.values():
        if isinstance(o.payload, DocumentSearchPayload | DocumentPayload):
            key = (o.payload.document_kind, o.committee_id)
            groups.setdefault(key, []).append(o)
    return tuple(_document_evidence(k, v) for k, v in sorted(groups.items()))


def _latest_hearings(hearings: Iterable[Observation]) -> list[Observation]:
    latest: dict[tuple[str | None, str], Observation] = {}
    for o in hearings:
        if not isinstance(o.payload, HearingPayload):
            continue
        key = (o.committee_id, o.payload.hearing_id)
        held = latest.get(key)
        if held is None or (o.observed_at, o.extractor_version) > (
            held.observed_at,
            held.extractor_version,
        ):
            latest[key] = o
    return list(latest.values())


def _end(tenure: Tenure) -> date | None:
    return (
        tenure.report.reported_on if tenure.report else None
    ) or tenure.discharged_on


def _strays(tenures: Iterable[Tenure]) -> dict[date, HearingNotice]:
    """Announcements for hearings set after their tenure ended, by hearing date.

    A committee taking over a bill can announce a hearing before the referral
    order is entered, which puts the row in the previous committee's tenure.
    """
    strays: dict[date, HearingNotice] = {}
    for tenure in tenures:
        end = _end(tenure)
        for n in tenure.hearing_notices:
            if (
                n.action_type == ActionType.HEARING_SCHEDULED
                and n.hearing_date is not None
                and end is not None
                and n.hearing_date > end
            ):
                strays.setdefault(n.hearing_date, n)
    return strays


def _release(tenure: Tenure, moved: set[HearingNotice]) -> Tenure:
    """Drop announcements adopted by a later tenure for hearings after this one."""
    end = _end(tenure)
    kept = tuple(
        n
        for n in tenure.hearing_notices
        if not (
            n in moved
            and end is not None
            and n.hearing_date is not None
            and n.hearing_date > end
        )
    )
    if len(kept) == len(tenure.hearing_notices):
        return tenure
    return replace(tenure, hearing_notices=kept)


def _with_dockets(
    bill: BillKey,
    tenure: Tenure,
    hearings: list[Observation],
    strays: dict[date, HearingNotice],
) -> tuple[Tenure, tuple[UUID, ...], tuple[HearingNotice, ...]]:
    """Add hearings whose docket lists the bill during this tenure.

    A stray announcement for the same hearing date is adopted in place of a
    bare docket listing; adopted announcements are returned.
    """
    end = _end(tenure)
    known = {n.hearing_date for n in tenure.hearing_notices}
    added: list[HearingNotice] = []
    used: list[UUID] = []
    adopted: list[HearingNotice] = []
    for o in hearings:
        payload = o.payload
        if (
            not isinstance(payload, HearingPayload)
            or o.committee_id != tenure.committee_id
            or bill.bill_id not in payload.docket
            or payload.hearing_date is None
            or payload.hearing_date < tenure.referred_on
            or (end is not None and payload.hearing_date > end)
            or payload.hearing_date in known
        ):
            continue
        known.add(payload.hearing_date)
        stray = strays.get(payload.hearing_date)
        if stray is not None and stray.announced_on is not None:
            added.append(stray)
            adopted.append(stray)
        else:
            added.append(
                HearingNotice(
                    announced_on=None,
                    action_type=ActionType.HEARING_SCHEDULED,
                    hearing_date=payload.hearing_date,
                    evidence=EvidenceRef(observation_id=o.observation_id),
                )
            )
        used.append(o.observation_id)
    if not added:
        return tenure, (), ()
    notices = tuple(
        sorted(
            (*tenure.hearing_notices, *added),
            key=lambda n: n.announced_on or n.hearing_date or date.min,
        )
    )
    return replace(tenure, hearing_notices=notices), tuple(used), tuple(adopted)


def project(
    bill: BillKey,
    observations: Iterable[Observation],
    hearings: Iterable[Observation] = (),
    as_of: datetime | None = None,
    vehicle_history: Callable[[str], Iterable[Observation]] | None = None,
    gaps: tuple[RecordGap, ...] = RECORD_GAPS,
) -> BillFacts:
    """Derive facts from a bill's observations and its committees' hearings.

    With as_of, only what had happened by then counts: history rows dated
    later are dropped, and document and hearing observations made later are
    ignored. History still comes from the latest capture, since each row
    carries its own event date. With vehicle_history, which gives a bill's
    observations by bill id, each bill this one accompanied is read for the
    committees that held it during the ride; without it, rides name none.
    gaps are the reviewed gaps in the record (record_gaps), applied only while
    the history itself shows no tenure before their committee.
    """
    observations = list(observations)
    hearings = list(hearings)
    if as_of is not None:
        hearings = [o for o in hearings if o.observed_at <= as_of]
    known = [
        o
        for o in observations
        if as_of is None
        or isinstance(o.payload, BillHistoryPayload)
        or o.observed_at <= as_of
    ]
    documents = _documents(known)
    current = _current_history(observations)
    if current is None:
        return BillFacts(bill, (), None, (), documents=documents)
    rows = _ordered_rows(current)
    cutoff = local_date(as_of) if as_of is not None else None
    if cutoff is not None:
        rows = [r for r in rows if r.action_date <= cutoff]
    own = gaps_for(gaps, bill.session, bill.bill_id)
    rows, stale = _supply_referrals(
        rows,
        (
            g
            for g in own
            if isinstance(g, SuppliedReferral)
            and (cutoff is None or g.anchor_on <= cutoff)
        ),
    )
    latest = _latest_hearings(hearings)
    sources = [current.observation_id]
    base = _tenures(bill, current, rows)
    strays = _strays(base)
    supplemented, moved = [], set()
    for tenure in base:
        tenure, used, adopted = _with_dockets(bill, tenure, latest, strays)
        supplemented.append(tenure)
        sources.extend(used)
        moved.update(adopted)
    tenures = [_release(t, moved) for t in supplemented]
    rides, carried_by = _rides(
        bill, current, rows, vehicle_history or (lambda _: ()), cutoff
    )
    sources.extend(carried_by)
    substituted, predecessors = _substitutions(
        current, rows, vehicle_history or (lambda _: ()), cutoff
    )
    rides += substituted
    sources.extend(predecessors)
    held = {t.committee_id for t in tenures}
    unrecorded = []
    for g in own:
        if not isinstance(g, UnrecordedTransfer):
            continue
        if g.committee_id in held:
            stale.append(g)
        elif cutoff is None or g.transferred_on <= cutoff:
            unrecorded.append(g)
    return BillFacts(
        bill=bill,
        tenures=tuple(tenures),
        last_observed_at=current.observed_at,
        source_observations=tuple(sources),
        origin=_origin(current, rows),
        documents=documents,
        rides=rides,
        unrecorded=tuple(unrecorded),
        stale_gaps=tuple(stale),
    )

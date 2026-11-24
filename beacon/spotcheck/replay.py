"""Re-evaluates a stored tenure with current code and gathers its source material."""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from bs4 import BeautifulSoup

from beacon.domain.evaluation import TenureEvaluation
from beacon.domain.ids import BillKey
from beacon.domain.inventory import BillRecord, CommitteeRecord
from beacon.domain.observations import HearingPayload, Observation, ObservationKind
from beacon.evaluator.clauses import ClauseIndex
from beacon.evaluator.engine import evaluate
from beacon.evaluator.facts import Tenure, project
from beacon.evaluator.rulesets.base import Ruleset
from beacon.ports.blobs import BlobStore
from beacon.ports.store import Store

_DOCUMENT_KINDS = (ObservationKind.DOCUMENT_SEARCH, ObservationKind.DOCUMENT)


@dataclass(frozen=True)
class RawRow:
    """The cell text of one table row on the captured bill page, in page order."""

    position: int
    cells: tuple[str, ...]


@dataclass(frozen=True)
class Case:
    """One tenure's evaluation by current code, with everything it was derived from.

    history is the bill-history observation the tenure came from (the newest one
    when no tenure was observed); raw_rows is what its capture literally said.
    hearings are the latest observation of each hearing whose docket lists the
    bill. searches and documents are scoped to the tenure's committee.
    """

    stored: TenureEvaluation
    evaluation: TenureEvaluation
    tenure: Tenure | None
    bill: BillRecord | None
    committee: CommitteeRecord | None
    history: Observation | None
    history_url: str | None
    raw_rows: tuple[RawRow, ...]
    hearings: tuple[Observation, ...]
    searches: tuple[Observation, ...]
    documents: tuple[Observation, ...]


def raw_rows(body: bytes) -> tuple[RawRow, ...]:
    """Every table row with at least three cells, as whitespace-normalized text."""
    soup = BeautifulSoup(body, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) >= 3:
            text = tuple(" ".join(c.get_text(" ", strip=True).split()) for c in cells)
            rows.append(RawRow(len(rows) + 1, text))
    return tuple(rows)


def _recency(observation: Observation) -> tuple[datetime, int]:
    return observation.observed_at, observation.extractor_version


def _newest(observations: list[Observation]) -> Observation | None:
    return max(observations, key=_recency, default=None)


class Replayer:
    """Replays stored evaluations at one moment under one ruleset."""

    def __init__(
        self,
        store: Store,
        blobs: BlobStore,
        clauses: ClauseIndex,
        ruleset: Ruleset,
        as_of: datetime,
        run_id: UUID,
    ) -> None:
        self._store = store
        self._blobs = blobs
        self._clauses = clauses
        self._ruleset = ruleset
        self._as_of = as_of
        self._run_id = run_id
        self._hearings: dict[str, list[Observation]] | None = None
        self._bills: dict[tuple[int, str], dict[str, BillRecord]] = {}

    def _committee_hearings(self, committee_id: str) -> list[Observation]:
        if self._hearings is None:
            self._hearings = {}
            for o in self._store.observations.of_kind(ObservationKind.HEARING):
                if o.committee_id is not None:
                    self._hearings.setdefault(o.committee_id, []).append(o)
        return self._hearings.get(committee_id, [])

    def _bill(self, session: int, committee_id: str, bill_id: str) -> BillRecord | None:
        key = (session, committee_id)
        if key not in self._bills:
            bills = self._store.inventory.bills_for(session, committee_id)
            self._bills[key] = {b.bill_id: b for b in bills}
        return self._bills[key].get(bill_id)

    def _docketed(self, committee_id: str, bill_id: str) -> tuple[Observation, ...]:
        latest: dict[str, tuple[Observation, HearingPayload]] = {}
        for o in self._committee_hearings(committee_id):
            payload = o.payload
            if not isinstance(payload, HearingPayload):
                continue
            held = latest.get(payload.hearing_id)
            if held is None or _recency(o) > _recency(held[0]):
                latest[payload.hearing_id] = (o, payload)
        docketed = sorted(
            (pair for pair in latest.values() if bill_id in pair[1].docket),
            key=lambda pair: (pair[1].hearing_date or date.max, pair[1].hearing_id),
        )
        return tuple(o for o, _ in docketed)

    def replay(self, stored: TenureEvaluation) -> Case | None:
        """The stored tenure evaluated by current code; None if the tenure is gone."""
        bill, committee_id = stored.bill, stored.committee_id
        observations = self._store.observations.for_bill(bill.session, bill.bill_id)
        facts = project(
            BillKey(bill.session, bill.bill_id),
            observations,
            self._committee_hearings(committee_id),
            as_of=self._as_of,
            vehicle_history=lambda b: self._store.observations.for_bill(
                bill.session, b, ObservationKind.BILL_HISTORY
            ),
        )
        evaluations = evaluate(
            facts, committee_id, self._ruleset, self._clauses, self._as_of, self._run_id
        )
        evaluation = next(
            (e for e in evaluations if e.tenure_id == stored.tenure_id), None
        )
        if evaluation is None:
            return None
        tenure = next(
            (t for t in facts.tenures if t.tenure_id == stored.tenure_id), None
        )
        if tenure is not None:
            history = self._store.observations.get(tenure.referral.observation_id)
        else:
            history = _newest(
                [o for o in observations if o.kind == ObservationKind.BILL_HISTORY]
            )
        url, rows = None, ()
        if history is not None:
            capture = self._store.captures.get(history.capture_id)
            url = capture.url
            if capture.content_hash is not None:
                rows = raw_rows(self._blobs.get(capture.content_hash))
        scoped = [
            o
            for o in observations
            if o.kind in _DOCUMENT_KINDS and o.committee_id == committee_id
        ]
        scoped.sort(key=lambda o: o.observed_at)
        return Case(
            stored=stored,
            evaluation=evaluation,
            tenure=tenure,
            bill=self._bill(bill.session, committee_id, bill.bill_id),
            committee=self._store.inventory.committee(bill.session, committee_id),
            history=history,
            history_url=url,
            raw_rows=rows,
            hearings=self._docketed(committee_id, bill.bill_id),
            searches=tuple(
                o for o in scoped if o.kind == ObservationKind.DOCUMENT_SEARCH
            ),
            documents=tuple(o for o in scoped if o.kind == ObservationKind.DOCUMENT),
        )

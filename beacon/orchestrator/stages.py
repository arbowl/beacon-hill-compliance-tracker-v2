"""The pipeline stages. Each opens a run, does its work, and emits events."""

import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

from beacon.adapters.events import EventBus
from beacon.adapters.site import normalized_hash
from beacon.adapters.sqlite_store import SqliteStore
from beacon.collector import committee_bills, committee_documents, hearings
from beacon.collector import committees as committee_lists
from beacon.config import Settings
from beacon.domain.captures import Capture
from beacon.domain.evaluation import RequirementStatus, TenureEvaluation
from beacon.domain.events import ItemFailed, Progress, RunFinished, RunStarted
from beacon.domain.evidence import EvidenceRef
from beacon.domain.ids import BillKey, CommitteeKey
from beacon.domain.inventory import BillRecord, CommitteeRecord
from beacon.domain.observations import (
    BillHistoryPayload,
    CommitteeDocument,
    CommitteeDocumentsPayload,
    DocumentKind,
    DocumentSearchPayload,
    HearingDocument,
    HearingPayload,
    Observation,
    ObservationKind,
)
from beacon.evaluator.clauses import ClauseIndex
from beacon.evaluator.engine import evaluate as evaluate_facts
from beacon.evaluator.facts import project
from beacon.evaluator.rulesets.base import Ruleset
from beacon.evaluator.rulesets.s194.ruleset import RULESET as S194
from beacon.ports.blobs import BlobStore
from beacon.ports.clock import Clock
from beacon.ports.events import EventSink
from beacon.ports.fetcher import Fetcher, FetchResult
from beacon.ports.store import RunStatus, Stage
from beacon.reporters.base import (
    CommitteeOutput,
    EvidenceItem,
    Reporter,
    ReportInput,
)
from beacon.reporters.evidence import describe
from beacon.scraper.documents import bill_tabs
from beacon.scraper.documents import observe as documents
from beacon.scraper.documents.observe import Fetched
from beacon.scraper.history import extractor as history
from beacon.scraper.history.committees import get_committee

RULESETS: dict[str, Ruleset] = {S194.ruleset_id: S194}
_BILL_URL = re.compile(r"/Bills/(\d+)/([HS]\d+)$", re.I)


class StageError(RuntimeError):
    """A stage could not complete."""


@dataclass(frozen=True)
class Context:
    """Everything a stage needs."""

    settings: Settings
    store: SqliteStore
    blobs: BlobStore
    fetcher: Fetcher
    clock: Clock
    clauses: ClauseIndex
    events: EventSink = field(default_factory=EventBus)


class Tracker:
    """Counts one run's items and emits progress events; thread-safe."""

    def __init__(
        self, ctx: Context, run_id: UUID, stage: Stage, total: int | None
    ) -> None:
        self.run_id = run_id
        self._ctx = ctx
        self._stage = stage
        self._total = total
        self._done = 0
        self._lock = threading.Lock()

    def step(self, item: str, outcome: str) -> None:
        """Record one finished item."""
        with self._lock:
            self._done += 1
            done = self._done
        self._ctx.events.emit(
            Progress(
                run_id=self.run_id,
                stage=self._stage.value,
                at=self._ctx.clock.now(),
                done=done,
                total=self._total,
                item=item,
                outcome=outcome,
            )
        )

    def fail(self, item: str, error: str) -> None:
        """Record one failed item; the run continues."""
        self._ctx.events.emit(
            ItemFailed(
                run_id=self.run_id,
                stage=self._stage.value,
                at=self._ctx.clock.now(),
                item=item,
                error=error,
            )
        )
        self.step(item, "failed")


def _code_version() -> str:
    return version("beacon")


def _run(
    ctx: Context,
    stage: Stage,
    work: Callable[[Tracker], dict[str, int]],
    scope: str | None = None,
    total: int | None = None,
) -> UUID:
    run_id = ctx.store.runs.start(
        stage, _code_version(), ctx.settings.fingerprint(), scope
    )
    ctx.events.emit(
        RunStarted(
            run_id=run_id,
            stage=stage.value,
            at=ctx.clock.now(),
            scope=scope,
            total=total,
        )
    )
    started = time.monotonic()

    def finish(status: RunStatus, stats: dict[str, int]) -> None:
        ctx.store.runs.finish(run_id, status, stats)
        ctx.events.emit(
            RunFinished(
                run_id=run_id,
                stage=stage.value,
                at=ctx.clock.now(),
                status=status.value,
                stats=stats,
                duration_s=time.monotonic() - started,
            )
        )

    try:
        stats = work(Tracker(ctx, run_id, stage, total))
    except BaseException:
        finish(RunStatus.FAILED, {})
        raise
    finish(RunStatus.SUCCEEDED, stats)
    return run_id


def _fetch(
    ctx: Context, url: str, run_id: UUID, headers: dict[str, str] | None = None
) -> tuple[FetchResult, bytes | None]:
    """Fetch and record a capture; return its body (stored or re-confirmed).

    A body matching the prior capture after normalization is recorded as
    re-confirming it, and is not stored again.
    """
    prior = ctx.store.captures.latest_for(url)
    result = ctx.fetcher.fetch(url, run_id, prior, headers=headers)
    capture, body = result.capture, result.body
    if body is not None:
        normalized = normalized_hash(body)
        prior_normalized = (
            normalized_hash(ctx.blobs.get(prior.content_hash))
            if prior is not None and prior.content_hash is not None
            else None
        )
        if prior is not None and prior_normalized == normalized:
            capture = capture.model_copy(
                update={
                    "content_hash": prior.content_hash,
                    "normalized_hash": normalized,
                    "not_modified_of": prior.capture_id,
                }
            )
        else:
            ctx.blobs.put(body)
            capture = capture.model_copy(update={"normalized_hash": normalized})
    elif capture.not_modified_of is not None and prior is not None:
        capture = capture.model_copy(update={"normalized_hash": prior.normalized_hash})
    ctx.store.captures.add(capture)
    if body is not None:
        return FetchResult(capture, body), body
    if capture.content_hash is not None:
        return FetchResult(capture, None), ctx.blobs.get(capture.content_hash)
    return FetchResult(capture, None), None


def _collect_listing(
    ctx: Context, key: CommitteeKey, tracker: Tracker, current: bool
) -> tuple[int, set[str]]:
    """Walk one listing (bills in committee, or bills that have left it)."""
    s = ctx.settings
    listing = "current" if current else "departed"
    page_number, last_page, fetched, seen = 1, 1, 0, set()
    while page_number <= last_page:
        fetched += 1
        url = committee_bills.listing_url(
            s.base_url, s.session, key.committee_id, page_number, current
        )
        result, body = _fetch(ctx, url, tracker.run_id, committee_bills.LISTING_HEADERS)
        if body is None:
            raise StageError(
                f"Listing page {page_number} (current={current}) failed with status "
                f"{result.capture.http_status}; inventory would be incomplete"
            )
        page = committee_bills.parse_listing(body, s.base_url, s.session)
        capture = result.capture
        if not ctx.store.observations.exists(
            capture.capture_id,
            committee_bills.EXTRACTOR_ID,
            committee_bills.EXTRACTOR_VERSION,
        ):
            ctx.store.observations.append_many(
                [committee_bills.extract(capture, page, s.session, key.committee_id)]
            )
        for bill in page.bills:
            ctx.store.inventory.upsert_bill(
                BillRecord(
                    session=s.session,
                    bill_id=bill.bill_id,
                    url=bill.url,
                    title=bill.title,
                )
            )
            ctx.store.inventory.link(
                s.session, key.committee_id, bill.bill_id, capture.requested_at
            )
            seen.add(bill.bill_id)
        tracker.step(f"{listing} page {page_number}", f"{listing}_pages")
        if not page.bills:
            break
        last_page = max(last_page, page.last_page)
        page_number += 1
    return fetched, seen


_DEFINITIVELY_ABSENT = (404, 410)


def _as_fetched(capture: Capture, body: bytes | None) -> Fetched | None:
    """A body, or an empty one for a definitive "not there"; None if inconclusive."""
    if body is not None:
        return Fetched(capture, body)
    if capture.http_status in _DEFINITIVELY_ABSENT:
        return Fetched(capture, b"")
    return None


def _fetch_once(ctx: Context, url: str, run_id: UUID) -> Fetched | None:
    """Documents do not change; reuse any stored capture instead of refetching."""
    prior = ctx.store.captures.latest_for(url)
    if prior is not None and prior.content_hash is not None:
        return Fetched(prior, ctx.blobs.get(prior.content_hash))
    result, body = _fetch(ctx, url, run_id)
    return _as_fetched(result.capture, body)


def _collect_hearings(ctx: Context, key: CommitteeKey, tracker: Tracker) -> int:
    """Record each hearing's docket and documents, reading packet PDFs once."""
    s = ctx.settings
    result, body = _fetch(
        ctx, hearings.hearings_url(s.base_url, key.committee_id), tracker.run_id
    )
    if body is None:
        raise StageError(
            f"Hearings list failed with status {result.capture.http_status}"
        )
    hearing_ids = hearings.parse_hearing_list(body)
    for hearing_id in hearing_ids:
        page_result, page_body = _fetch(
            ctx, hearings.hearing_url(s.base_url, hearing_id), tracker.run_id
        )
        if page_body is None:
            status = page_result.capture.http_status
            tracker.fail(f"hearing {hearing_id}", f"HTTP {status}")
            continue
        page = hearings.parse_hearing(page_body, s.base_url, hearing_id)
        consulted, documents_found = [], []
        for listed in page.documents:
            document = _fetch_once(ctx, listed.url, tracker.run_id)
            if document is None:
                tracker.fail(listed.title, "document fetch failed")
            else:
                consulted.append(document.capture)
            body = document.body if document else None
            documents_found.append(hearings.hearing_document(listed, page.docket, body))
        capture = page_result.capture
        if not ctx.store.observations.exists(
            capture.capture_id, hearings.EXTRACTOR_ID, hearings.EXTRACTOR_VERSION
        ):
            ctx.store.observations.append_many(
                [
                    hearings.extract(
                        capture,
                        page,
                        tuple(documents_found),
                        tuple(consulted),
                        s.session,
                        key.committee_id,
                    )
                ]
            )
        tracker.step(f"hearing {hearing_id}", "hearings")
    return len(hearing_ids)


def _collect_committee_documents(
    ctx: Context, key: CommitteeKey, tracker: Tracker
) -> int:
    """Record the committee's Documents tab, reading each listed document once."""
    s = ctx.settings
    url = committee_documents.documents_url(s.base_url, key.committee_id, s.session)
    result, body = _fetch(ctx, url, tracker.run_id)
    if body is None:
        raise StageError(
            f"Documents tab failed with status {result.capture.http_status}"
        )
    listed = committee_documents.parse_documents_tab(body, s.base_url)
    consulted, found = [], []
    for item in listed:
        document = _fetch_once(ctx, item.url, tracker.run_id)
        if document is None:
            tracker.fail(item.title, "document fetch failed")
        else:
            consulted.append(document.capture)
        found.append(
            committee_documents.committee_document(
                item, document.body if document else None
            )
        )
    capture = result.capture
    if not ctx.store.observations.exists(
        capture.capture_id,
        committee_documents.EXTRACTOR_ID,
        committee_documents.EXTRACTOR_VERSION,
    ):
        ctx.store.observations.append_many(
            [
                committee_documents.extract(
                    capture, tuple(found), tuple(consulted), s.session, key.committee_id
                )
            ]
        )
    tracker.step("documents tab", "committee_documents")
    return len(listed)


def collect_committees(ctx: Context) -> list[str]:
    """Record every committee listed for each chamber; returns their ids."""
    s = ctx.settings
    found: list[str] = []

    def work(tracker: Tracker) -> dict[str, int]:
        for chamber in committee_lists.LISTINGS:
            url = committee_lists.listing_url(s.base_url, chamber)
            result, body = _fetch(ctx, url, tracker.run_id)
            if body is None:
                raise StageError(
                    f"Committee listing {url} failed with status "
                    f"{result.capture.http_status}"
                )
            for record in committee_lists.parse_committee_list(
                body, s.base_url, s.session, chamber
            ):
                ctx.store.inventory.upsert_committee(record)
                found.append(record.committee_id)
            tracker.step(chamber.value, "listed")
        return {"committees": len(found)}

    _run(ctx, Stage.COLLECT, work, scope="committees", total=3)
    return found


def collect(ctx: Context, committee_id: str) -> UUID:
    """Record the committee, every bill listed for it, its hearings and documents.

    The site lists bills still in committee and bills that have left it
    separately; inventory is the union.
    """
    s = ctx.settings
    key = CommitteeKey(s.session, committee_id)
    alias = get_committee(key.committee_id)
    ctx.store.inventory.upsert_committee(
        CommitteeRecord(
            session=s.session,
            committee_id=key.committee_id,
            name=alias.canonical_name if alias else key.committee_id,
            chamber=key.chamber,
            url=f"{s.base_url}/Committees/Detail/{key.committee_id}",
        )
    )

    def work(tracker: Tracker) -> dict[str, int]:
        pages, seen = 0, set()
        for current in (True, False):
            listing_pages, bill_ids = _collect_listing(ctx, key, tracker, current)
            pages += listing_pages
            seen |= bill_ids
        heard = _collect_hearings(ctx, key, tracker)
        posted = _collect_committee_documents(ctx, key, tracker)
        return {
            "pages": pages,
            "bills": len(seen),
            "hearings": heard,
            "committee_documents": posted,
        }

    return _run(ctx, Stage.COLLECT, work, scope=key.committee_id)


def _hearing_index(
    ctx: Context, committee_id: str, as_of: datetime | None = None
) -> dict[str, list[HearingDocument]] | None:
    """Hearing documents per bill, from each hearing's latest observation.

    None when no hearing of the committee had been observed by as_of.
    """
    latest: dict[str, Observation] = {}
    for o in ctx.store.observations.of_kind(ObservationKind.HEARING):
        if o.committee_id != committee_id or not isinstance(o.payload, HearingPayload):
            continue
        if as_of is not None and o.observed_at > as_of:
            continue
        held = latest.get(o.payload.hearing_id)
        if held is None or o.observed_at >= held.observed_at:
            latest[o.payload.hearing_id] = o
    if not latest:
        return None
    index: dict[str, list[HearingDocument]] = {}
    for o in latest.values():
        assert isinstance(o.payload, HearingPayload)
        for document in o.payload.documents:
            for bill_id in document.bill_ids:
                index.setdefault(bill_id, []).append(document)
    return index


def _committee_document_index(
    ctx: Context, committee_id: str, as_of: datetime | None = None
) -> dict[str, list[CommitteeDocument]] | None:
    """Documents tab entries per bill, from the latest observation by as_of.

    None when the committee's Documents tab had not been observed by as_of.
    """
    latest: Observation | None = None
    for o in ctx.store.observations.of_kind(ObservationKind.COMMITTEE_DOCUMENTS):
        if o.committee_id != committee_id:
            continue
        if as_of is not None and o.observed_at > as_of:
            continue
        if latest is None or o.observed_at >= latest.observed_at:
            latest = o
    if latest is None:
        return None
    assert isinstance(latest.payload, CommitteeDocumentsPayload)
    index: dict[str, list[CommitteeDocument]] = {}
    for document in latest.payload.documents:
        for bill_id in document.bill_ids:
            index.setdefault(bill_id, []).append(document)
    return index


def _store_new(ctx: Context, observations: list[Observation]) -> None:
    ctx.store.observations.append_many(
        [
            o
            for o in observations
            if not ctx.store.observations.exists(
                o.capture_id, o.extractor_id, o.extractor_version
            )
        ]
    )


class _TabCache:
    """Fetches each vote tab at most once per run; many bills can share one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._fetched: dict[str, Fetched | None] = {}

    def get(self, ctx: Context, url: str, run_id: UUID) -> Fetched | None:
        with self._lock:
            url_lock = self._locks.setdefault(url, threading.Lock())
        with url_lock:
            if url not in self._fetched:
                result, body = _fetch(ctx, url, run_id)
                self._fetched[url] = _as_fetched(result.capture, body)
            return self._fetched[url]


@dataclass(frozen=True)
class _ScrapeShared:
    """State shared by every bill in one scrape run."""

    hearing_index: dict[str, list[HearingDocument]] | None
    committee_document_index: dict[str, list[CommitteeDocument]] | None
    tab_cache: _TabCache
    open_bills: frozenset[str]
    last_full: dict[str, datetime]
    sweep_before: datetime
    full: bool

    def document_reason(self, bill_id: str, page_changed: bool) -> str | None:
        """Why to run the document finders for a bill, or None to skip them."""
        if self.full:
            return "full"
        if page_changed:
            return "page_changed"
        if bill_id in self.open_bills:
            return "open_obligation"
        last = self.last_full.get(bill_id)
        if last is None or last < self.sweep_before:
            return "sweep"
        return None


def _for_bill[T](index: dict[str, list[T]] | None, bill_id: str) -> list[T] | None:
    """A bill's entries; None (not an empty list) when the source was never read."""
    return None if index is None else index.get(bill_id, [])


def _observe_documents(
    ctx: Context,
    bill: BillRecord,
    committee_id: str,
    capture: Capture,
    body: bytes,
    history_payload: BillHistoryPayload,
    shared: _ScrapeShared,
    tracker: Tracker,
) -> None:
    document_plan = documents.plan(
        body,
        bill.url,
        ctx.settings.base_url,
        bill.session,
        history_payload,
        committee_id,
    )
    fetched: dict[str, Fetched] = {}
    for url in document_plan.urls:
        if url.endswith(("/CommitteeVote", "/CommitteeSummary")):
            document = shared.tab_cache.get(ctx, url, tracker.run_id)
        else:
            document = _fetch_once(ctx, url, tracker.run_id)
        if document is not None:
            fetched[url] = document
    _store_new(
        ctx,
        documents.observe(
            capture,
            body,
            document_plan,
            fetched,
            _for_bill(shared.hearing_index, bill.bill_id),
            _for_bill(shared.committee_document_index, bill.bill_id),
            bill.session,
            bill.bill_id,
            committee_id,
        ),
    )


def _scrape_bill(
    ctx: Context,
    bill: BillRecord,
    committee_id: str,
    shared: _ScrapeShared,
    tracker: Tracker,
) -> str:
    try:
        result, body = _fetch(ctx, bill.url, tracker.run_id)
    except Exception as error:
        tracker.fail(bill.bill_id, repr(error))
        return "failed"
    if body is None:
        tracker.fail(bill.bill_id, f"HTTP {result.capture.http_status}")
        return "failed"
    capture = result.capture
    observation = history.extract(capture, body, bill.session, bill.bill_id)
    _store_new(ctx, [observation])
    assert isinstance(observation.payload, BillHistoryPayload)
    page_changed = capture.not_modified_of is None
    if shared.document_reason(bill.bill_id, page_changed) is None:
        tracker.step(bill.bill_id, "unchanged_documents_skipped")
        return "unchanged_documents_skipped"
    try:
        _observe_documents(
            ctx,
            bill,
            committee_id,
            capture,
            body,
            observation.payload,
            shared,
            tracker,
        )
    except Exception as error:
        tracker.fail(bill.bill_id, f"documents: {error!r}")
        return "failed"
    outcome = "unchanged" if capture.not_modified_of is not None else "changed"
    tracker.step(bill.bill_id, outcome)
    return outcome


def _open_bills(ctx: Context, committee_id: str) -> frozenset[str]:
    """Bills whose latest evaluation has a Provisional requirement."""
    s = ctx.settings
    run_id = ctx.store.evaluations.latest_run(s.session, committee_id, s.ruleset)
    if run_id is None:
        return frozenset()
    return frozenset(
        e.bill.bill_id
        for e in ctx.store.evaluations.for_run(run_id)
        if any(r.status == RequirementStatus.PROVISIONAL for r in e.results)
    )


def _last_full_searches(ctx: Context, committee_id: str) -> dict[str, datetime]:
    """When each bill last had an exhaustive document search."""
    last: dict[str, datetime] = {}
    for o in ctx.store.observations.of_kind(ObservationKind.DOCUMENT_SEARCH):
        payload = o.payload
        if (
            o.committee_id == committee_id
            and o.bill_id
            and isinstance(payload, DocumentSearchPayload)
            and payload.exhaustive
            and o.extractor_version >= bill_tabs.FINDER_VERSION
        ):
            last[o.bill_id] = max(o.observed_at, last.get(o.bill_id, o.observed_at))
    return last


def scrape(ctx: Context, committee_id: str, full: bool = False) -> UUID:
    """Capture every inventoried bill's page; extract history and documents.

    Document finders run when the page changed, an obligation is open, the
    weekly sweep is due, or full is set; otherwise they are skipped.
    """
    bills = ctx.store.inventory.bills_for(ctx.settings.session, committee_id)
    shared = _ScrapeShared(
        hearing_index=_hearing_index(ctx, committee_id),
        committee_document_index=_committee_document_index(ctx, committee_id),
        tab_cache=_TabCache(),
        open_bills=_open_bills(ctx, committee_id),
        last_full=_last_full_searches(ctx, committee_id),
        sweep_before=ctx.clock.now() - timedelta(days=ctx.settings.schedule.sweep_days),
        full=full,
    )

    def work(tracker: Tracker) -> dict[str, int]:
        with ThreadPoolExecutor(ctx.settings.fetch.max_workers) as pool:
            outcomes = list(
                pool.map(
                    lambda b: _scrape_bill(ctx, b, committee_id, shared, tracker),
                    bills,
                )
            )
        return dict(Counter(outcomes)) | {"bills": len(bills)}

    return _run(ctx, Stage.SCRAPE, work, scope=committee_id, total=len(bills))


def _stale_document_sources(ctx: Context) -> dict[tuple[UUID, str], tuple[UUID, ...]]:
    """Bill captures whose vote searches predate the current finder version."""
    sources: dict[tuple[UUID, str], tuple[UUID, ...]] = {}
    for o in ctx.store.observations.of_kind(ObservationKind.DOCUMENT_SEARCH):
        payload = o.payload
        if (
            o.committee_id
            and isinstance(payload, DocumentSearchPayload)
            and payload.document_kind == DocumentKind.VOTES
            and o.extractor_version < bill_tabs.FINDER_VERSION
        ):
            sources[(o.capture_id, o.committee_id)] = payload.consulted
    return sources


def _reextract_documents(
    ctx: Context, capture_id: UUID, committee_id: str, consulted: tuple[UUID, ...]
) -> bool:
    """Re-derive one bill capture's document observations; False if current."""
    search_id = documents.search_extractor_id(DocumentKind.VOTES, committee_id)
    if ctx.store.observations.exists(capture_id, search_id, bill_tabs.FINDER_VERSION):
        return False
    bill_capture = ctx.store.captures.get(capture_id)
    match = _BILL_URL.search(bill_capture.url)
    if match is None or bill_capture.content_hash is None:
        return False
    session, bill_id = int(match.group(1)), match.group(2)
    body = ctx.blobs.get(bill_capture.content_hash)
    payload = history.parse_history(body, bill_id)
    fetched: dict[str, Fetched] = {}
    for source_id in consulted[1:]:
        source = ctx.store.captures.get(source_id)
        source_body = (
            ctx.blobs.get(source.content_hash) if source.content_hash else None
        )
        document = _as_fetched(source, source_body)
        if document is not None:
            fetched[source.url] = document
    document_plan = documents.plan(
        body, bill_capture.url, ctx.settings.base_url, session, payload, committee_id
    )
    for url in document_plan.urls:
        if url not in fetched and not url.endswith(
            ("/CommitteeVote", "/CommitteeSummary")
        ):
            stored = ctx.store.captures.latest_for(url)
            if stored is not None and stored.content_hash is not None:
                fetched[url] = Fetched(stored, ctx.blobs.get(stored.content_hash))
    at = bill_capture.requested_at
    _store_new(
        ctx,
        documents.observe(
            bill_capture,
            body,
            document_plan,
            fetched,
            _for_bill(_hearing_index(ctx, committee_id, as_of=at), bill_id),
            _for_bill(_committee_document_index(ctx, committee_id, as_of=at), bill_id),
            session,
            bill_id,
            committee_id,
        ),
    )
    return True


def _stale_hearings(ctx: Context) -> list[Observation]:
    """Hearing observations older than the current extractor, one per capture."""
    stale: dict[UUID, Observation] = {}
    for o in ctx.store.observations.of_kind(ObservationKind.HEARING):
        if o.extractor_version < hearings.EXTRACTOR_VERSION:
            stale[o.capture_id] = o
    return list(stale.values())


def _reextract_hearing(ctx: Context, old: Observation) -> bool:
    """Rebuild one hearing observation from stored captures; False if current."""
    if ctx.store.observations.exists(
        old.capture_id, hearings.EXTRACTOR_ID, hearings.EXTRACTOR_VERSION
    ):
        return False
    assert isinstance(old.payload, HearingPayload) and old.committee_id
    capture = ctx.store.captures.get(old.capture_id)
    if capture.content_hash is None:
        return False
    page = hearings.parse_hearing(
        ctx.blobs.get(capture.content_hash),
        ctx.settings.base_url,
        old.payload.hearing_id,
    )
    consulted, found = [], []
    for listed in page.documents:
        stored = ctx.store.captures.latest_for(listed.url)
        body = None
        if stored is not None and stored.content_hash is not None:
            consulted.append(stored)
            body = ctx.blobs.get(stored.content_hash)
        found.append(hearings.hearing_document(listed, page.docket, body))
    _store_new(
        ctx,
        [
            hearings.extract(
                capture,
                page,
                tuple(found),
                tuple(consulted),
                old.session,
                old.committee_id,
            )
        ],
    )
    return True


def _stale_committee_documents(ctx: Context) -> list[Observation]:
    """Documents tab observations older than the current extractor, one per capture."""
    stale: dict[UUID, Observation] = {}
    for o in ctx.store.observations.of_kind(ObservationKind.COMMITTEE_DOCUMENTS):
        if o.extractor_version < committee_documents.EXTRACTOR_VERSION:
            stale[o.capture_id] = o
    return list(stale.values())


def _reextract_committee_documents(ctx: Context, old: Observation) -> bool:
    """Rebuild one Documents tab observation from stored captures; False if current."""
    if ctx.store.observations.exists(
        old.capture_id,
        committee_documents.EXTRACTOR_ID,
        committee_documents.EXTRACTOR_VERSION,
    ):
        return False
    assert old.committee_id
    capture = ctx.store.captures.get(old.capture_id)
    if capture.content_hash is None:
        return False
    listed = committee_documents.parse_documents_tab(
        ctx.blobs.get(capture.content_hash), ctx.settings.base_url
    )
    consulted, found = [], []
    for item in listed:
        stored = ctx.store.captures.latest_for(item.url)
        body = None
        if stored is not None and stored.content_hash is not None:
            consulted.append(stored)
            body = ctx.blobs.get(stored.content_hash)
        found.append(committee_documents.committee_document(item, body))
    _store_new(
        ctx,
        [
            committee_documents.extract(
                capture, tuple(found), tuple(consulted), old.session, old.committee_id
            )
        ],
    )
    return True


def reextract(ctx: Context) -> UUID:
    """Re-run the current extractors over every stored capture, offline.

    Hearings and Documents tabs are rebuilt before documents, which depend on them.
    """
    prefix = f"{ctx.settings.base_url}/Bills/"
    captures = [
        c
        for c in ctx.store.captures.iter_with_content(prefix)
        if c.content_hash is not None and _BILL_URL.search(c.url)
    ]
    stale_hearings = _stale_hearings(ctx)
    stale_tabs = _stale_committee_documents(ctx)
    stale = _stale_document_sources(ctx)

    def work(tracker: Tracker) -> dict[str, int]:
        added = skipped = documents_added = hearings_added = tabs_added = 0
        for old in stale_hearings:
            if _reextract_hearing(ctx, old):
                hearings_added += 1
            tracker.step(str(old.capture_id), "hearings")
        for old in stale_tabs:
            if _reextract_committee_documents(ctx, old):
                tabs_added += 1
            tracker.step(str(old.capture_id), "committee_documents")
        for capture in captures:
            match = _BILL_URL.search(capture.url)
            if match is None or capture.content_hash is None:
                continue
            if ctx.store.observations.exists(
                capture.capture_id, history.EXTRACTOR_ID, history.EXTRACTOR_VERSION
            ):
                skipped += 1
                tracker.step(match.group(2), "skipped")
                continue
            body = ctx.blobs.get(capture.content_hash)
            obs = history.extract(capture, body, int(match.group(1)), match.group(2))
            ctx.store.observations.append_many([obs])
            added += 1
            tracker.step(match.group(2), "added")
        for (capture_id, committee_id), consulted in stale.items():
            if _reextract_documents(ctx, capture_id, committee_id, consulted):
                documents_added += 1
                tracker.step(str(capture_id), "documents_added")
            else:
                tracker.step(str(capture_id), "skipped")
        return {
            "added": added,
            "skipped": skipped,
            "documents_added": documents_added,
            "hearings_added": hearings_added,
            "committee_documents_added": tabs_added,
        }

    scope = (
        f"history v{history.EXTRACTOR_VERSION}, "
        f"hearings v{hearings.EXTRACTOR_VERSION}, "
        f"committee documents v{committee_documents.EXTRACTOR_VERSION}, "
        f"documents v{bill_tabs.FINDER_VERSION}"
    )
    total = len(stale_hearings) + len(stale_tabs) + len(captures) + len(stale)
    return _run(ctx, Stage.REEXTRACT, work, scope=scope, total=total)


def _committee_hearings(ctx: Context, committee_id: str) -> list[Observation]:
    return [
        o
        for o in ctx.store.observations.of_kind(ObservationKind.HEARING)
        if o.committee_id == committee_id
    ]


def evaluate(ctx: Context, committee_id: str, as_of: datetime | None = None) -> UUID:
    """Evaluate every inventoried bill from stored observations only."""
    s = ctx.settings
    ruleset = RULESETS[s.ruleset]
    moment = as_of or ctx.clock.now()
    bills = ctx.store.inventory.bills_for(s.session, committee_id)
    hearing_pages = _committee_hearings(ctx, committee_id)

    def work(tracker: Tracker) -> dict[str, int]:
        labels: Counter[str] = Counter()
        for bill in bills:
            observations = ctx.store.observations.for_bill(s.session, bill.bill_id)
            facts = project(
                BillKey(bill.session, bill.bill_id),
                observations,
                hearing_pages,
                as_of=moment,
            )
            evaluations = evaluate_facts(
                facts, committee_id, ruleset, ctx.clauses, moment, tracker.run_id
            )
            ctx.store.evaluations.append_many(evaluations)
            labels.update(e.label.value for e in evaluations)
            outcome = evaluations[-1].label.value if evaluations else "committee_output"
            if not evaluations:
                labels["committee_output"] += 1
            tracker.step(bill.bill_id, outcome)
        return dict(labels) | {"bills": len(bills)}

    return _run(ctx, Stage.EVALUATE, work, scope=committee_id, total=len(bills))


def _committee_output(
    ctx: Context, bills: list[BillRecord], evaluations: list[TenureEvaluation]
) -> list[CommitteeOutput]:
    """Inventoried documents without evaluations that the committee produced."""
    evaluated = {e.bill.bill_id for e in evaluations}
    output = []
    for bill in bills:
        if bill.bill_id in evaluated:
            continue
        observations = ctx.store.observations.for_bill(
            bill.session, bill.bill_id, ObservationKind.BILL_HISTORY
        )
        origin = project(BillKey(bill.session, bill.bill_id), observations).origin
        if origin is not None:
            output.append(
                CommitteeOutput(
                    bill.bill_id, bill.title, origin.originated_on, origin.source_bills
                )
            )
    return output


def _evidence(
    ctx: Context, evaluations: list[TenureEvaluation]
) -> dict[EvidenceRef, EvidenceItem]:
    """Resolve every cited reference; ones whose records are gone are left out."""
    refs = dict.fromkeys(
        ref for e in evaluations for r in e.results for ref in r.evidence
    )
    observations: dict[UUID, Observation | None] = {}
    resolved: dict[EvidenceRef, EvidenceItem] = {}
    for ref in refs:
        if ref.observation_id not in observations:
            try:
                observations[ref.observation_id] = ctx.store.observations.get(
                    ref.observation_id
                )
            except KeyError:
                observations[ref.observation_id] = None
        observation = observations[ref.observation_id]
        if observation is None:
            continue
        try:
            capture = ctx.store.captures.get(observation.capture_id)
        except KeyError:
            continue
        resolved[ref] = describe(ref, observation, capture)
    return resolved


def report(ctx: Context, committee_id: str, reporters: list[Reporter]) -> list[Path]:
    """Hand the latest successful evaluation run to each reporter.

    A committee with no bills has nothing to report; one with bills but no
    evaluation is an error.
    """
    s = ctx.settings
    bills = ctx.store.inventory.bills_for(s.session, committee_id)
    if not bills:
        return []
    run_id = ctx.store.evaluations.latest_run(s.session, committee_id, s.ruleset)
    if run_id is None:
        raise StageError(f"No successful evaluation for {committee_id}")
    committee = ctx.store.inventory.committee(s.session, committee_id)
    evaluations = ctx.store.evaluations.for_run(run_id)
    payload = ReportInput(
        session=s.session,
        committee_id=committee_id,
        committee_name=committee.name if committee else None,
        evaluations=evaluations,
        bills=bills,
        committee_output=_committee_output(ctx, bills, evaluations),
        evidence=_evidence(ctx, evaluations),
        committee_url=committee.url if committee else None,
    )
    written: list[Path] = []

    def work(tracker: Tracker) -> dict[str, int]:
        for reporter in reporters:
            written.extend(reporter.write(payload, s.report_dir))
            tracker.step(type(reporter).__name__, "written")
        return {"files": len(written)}

    _run(ctx, Stage.REPORT, work, scope=committee_id, total=len(reporters))
    return written

"""End-to-end stage runs against captured J19 pages."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from beacon.adapters.blobs import FileBlobStore
from beacon.adapters.clock import FixedClock
from beacon.adapters.fetchers import FixtureFetcher
from beacon.adapters.sqlite_store import SqliteStore
from beacon.collector import committee_documents, hearings
from beacon.collector.committee_bills import listing_url
from beacon.config import Settings
from beacon.domain.evaluation import RequirementStatus, StepRole
from beacon.domain.events import (
    CommitteeStarted,
    ItemFailed,
    PlanFinished,
    PlanStarted,
    Progress,
    RunEvent,
    RunFinished,
    RunStarted,
)
from beacon.domain.observations import CommitteeDocumentsPayload, ObservationKind
from beacon.evaluator.clauses import ClauseIndex
from beacon.orchestrator import runner, stages
from beacon.orchestrator.runner import RunRequest
from beacon.orchestrator.stages import Context, StageError
from beacon.ports.fetcher import FetchResult
from beacon.ports.store import RunStatus, Stage
from beacon.reporters.json_export import JsonReporter
from beacon.scraper.documents import bill_tabs
from beacon.scraper.history import extractor as history

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
BASE = "https://malegislature.gov"
BILLS = ("H1768", "H1648", "H67", "S1168", "S1043", "H4783")
COMMITTEE_DOCUMENTS = ("H4372", "H5281")
AS_OF = datetime(2026, 11, 18, 17, tzinfo=UTC)
DOCUMENTS_TAB = committee_documents.documents_url(BASE, "J19", 194)


def listing_html(bill_ids: tuple[str, ...]) -> bytes:
    rows = "".join(
        f'<tr><td></td><td><a href="/Bills/194/{b}">{b[0]}.{b[1:]}</a></td>'
        f"<td>An Act {b}</td><td>Sponsor</td></tr>"
        for b in bill_ids
    )
    return f"<table><tbody>{rows}</tbody></table>".encode()


def fixture_pages() -> dict[str, bytes]:
    pages = {
        listing_url(BASE, 194, "J19", 1, True): listing_html(BILLS[:2]),
        listing_url(BASE, 194, "J19", 1, False): listing_html(
            BILLS[2:] + COMMITTEE_DOCUMENTS
        ),
    }
    pages[f"{BASE}/Committees/Detail/J19/Hearings"] = (
        b'<a href="/Events/Hearings/Detail/5123">Hearing</a>'
    )
    pages[f"{BASE}/Events/Hearings/Detail/5123"] = (
        PAGES / "hearing_5123.html"
    ).read_bytes()
    pages[DOCUMENTS_TAB] = (PAGES / "J19_Documents.html").read_bytes()
    pages[f"{BASE}/Reports/22939/Committee%20-%20Senate%20member%20votes.pdf"] = (
        PAGES / "report_22939.pdf"
    ).read_bytes()
    pages[f"{BASE}/Bills/194/H1768/CommitteeVote"] = (
        PAGES / "H1768_CommitteeVote.html"
    ).read_bytes()
    for b in BILLS + COMMITTEE_DOCUMENTS:
        pages[f"{BASE}/Bills/194/{b}"] = (PAGES / f"{b}.html").read_bytes()
    return pages


class OfflineFetcher:
    def fetch(self, url, run_id, prior=None, headers=None) -> FetchResult:
        raise AssertionError(f"network used for {url}")


@pytest.fixture
def ctx(tmp_path: Path, clause_index: ClauseIndex):
    settings = Settings(data_dir=tmp_path, base_url=BASE, committees=("J19",))
    clock = FixedClock(AS_OF)
    store = SqliteStore(settings.db_path)
    context = Context(
        settings=settings,
        store=store,
        blobs=FileBlobStore(settings.blob_dir),
        fetcher=FixtureFetcher(clock, fixture_pages()),
        clock=clock,
        clauses=clause_index,
    )
    yield context
    store.close()


def labels(ctx: Context, run_id: UUID) -> dict[str, str]:
    return {
        e.bill.bill_id: e.label.value for e in ctx.store.evaluations.for_run(run_id)
    }


EXPECTED = {
    "H1768": "Non-Compliant",
    "H1648": "Non-Compliant",
    "H67": "Non-Compliant",
    "S1168": "Compliant",
    "S1043": "Non-Compliant",
    "H4783": "Non-Compliant",
}


def test_full_pipeline(ctx: Context):
    stages.collect(ctx, "J19")
    assert len(ctx.store.inventory.bills_for(194, "J19")) == 8
    stages.scrape(ctx, "J19")
    run_id = stages.evaluate(ctx, "J19")
    assert labels(ctx, run_id) == EXPECTED
    (path,) = stages.report(ctx, "J19", [JsonReporter()])
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["summary"] == {"Non-Compliant": 5, "Compliant": 1}
    h67 = next(b for b in document["bills"] if b["bill_id"] == "H67")
    h67_results = {r["requirement_id"]: r for r in h67["tenures"][0]["requirements"]}
    assert h67_results["summary_posted"]["status"] == "Met"
    assert h67_results["votes_posted"]["status"] == "Unmet"
    assert "No vote record found" in h67_results["votes_posted"]["detail"]
    output = {o["bill_id"]: o for o in document["committee_output"]}
    assert set(output) == set(COMMITTEE_DOCUMENTS)
    assert output["H4372"]["source_bills"] == ["H1768"]
    s1168 = next(b for b in document["bills"] if b["bill_id"] == "S1168")
    report_out = s1168["tenures"][0]["requirements"][0]
    assert report_out["requirement_id"] == "report_out_deadline"
    assert {c["ref"]["rule"] for c in report_out["clauses"]} == {"10"}
    results = {r["requirement_id"]: r for r in s1168["tenures"][0]["requirements"]}
    assert results["summary_posted"]["status"] == "Met"
    assert "presumed" in results["summary_posted"]["detail"]
    assert results["votes_posted"]["status"] == "Met"
    assert results["hearing_notice"]["status"] == "Met"


def _noisy(body: bytes, night: int) -> bytes:
    token = f'<input name="__RequestVerificationToken" type="hidden" value="t{night}">'
    weather = (
        '<div class="visible-lg-inline weatherBox">'
        f'<span class="weatherDate">November {night}, 2026</span>'
        f'<span class="weatherStats">{50 + night}&deg;F</span></div>'
    )
    return body + (token + weather).encode()


def test_second_night_confirms_unchanged_despite_noise(ctx: Context):
    assert isinstance(ctx.fetcher, FixtureFetcher)
    assert isinstance(ctx.clock, FixedClock)
    pages = dict(ctx.fetcher.pages)
    ctx.fetcher.pages = {u: _noisy(b, 1) for u, b in pages.items()}
    stages.collect(ctx, "J19")
    stages.scrape(ctx, "J19")
    blob_count = sum(1 for p in ctx.settings.blob_dir.rglob("*") if p.is_file())
    ctx.clock.current = AS_OF + timedelta(days=1)
    ctx.fetcher.pages = {u: _noisy(b, 2) for u, b in pages.items()}
    stats_run = stages.scrape(ctx, "J19")
    captures = [
        c
        for c in ctx.store.captures.iter_with_content(f"{BASE}/Bills/")
        if c.url.endswith("/H67")
    ]
    assert [c.http_status for c in captures] == [200, 200]
    assert captures[1].not_modified_of == captures[0].capture_id
    assert captures[1].content_hash == captures[0].content_hash
    assert sum(1 for p in ctx.settings.blob_dir.rglob("*") if p.is_file()) == blob_count
    assert ctx.store.runs.status(stats_run) == RunStatus.SUCCEEDED
    observations = ctx.store.observations.for_bill(
        194, "H67", ObservationKind.BILL_HISTORY
    )
    assert observations[-1].observed_at == AS_OF + timedelta(days=1)


def test_offline_reevaluation_and_reextraction(ctx: Context, monkeypatch):
    stages.collect(ctx, "J19")
    stages.scrape(ctx, "J19")
    offline = replace(ctx, fetcher=OfflineFetcher())
    first = stages.evaluate(offline, "J19")
    assert labels(offline, first) == EXPECTED
    current = history.EXTRACTOR_VERSION
    monkeypatch.setattr(history, "EXTRACTOR_VERSION", current + 1)
    reextract_run = stages.reextract(offline)
    observations = ctx.store.observations.for_bill(
        194, "H67", ObservationKind.BILL_HISTORY
    )
    assert {o.extractor_version for o in observations} == {current, current + 1}
    assert ctx.store.runs.status(reextract_run) == RunStatus.SUCCEEDED
    second = stages.evaluate(offline, "J19")
    before = {
        e.bill.bill_id: e.input_fingerprint
        for e in ctx.store.evaluations.for_run(first)
    }
    after = {
        e.bill.bill_id: e.input_fingerprint
        for e in ctx.store.evaluations.for_run(second)
    }
    assert all(before[b] != after[b] for b in BILLS)


def test_reextract_is_idempotent(ctx: Context):
    stages.collect(ctx, "J19")
    stages.scrape(ctx, "J19")
    before = len(ctx.store.observations.for_bill(194, "H67"))
    stages.reextract(ctx)
    assert len(ctx.store.observations.for_bill(194, "H67")) == before


def test_failed_listing_fails_the_run(ctx: Context):
    assert isinstance(ctx.fetcher, FixtureFetcher)
    ctx.fetcher.pages.clear()
    with pytest.raises(StageError):
        stages.collect(ctx, "J19")


def test_failed_documents_tab_fails_the_run(ctx: Context):
    assert isinstance(ctx.fetcher, FixtureFetcher)
    del ctx.fetcher.pages[DOCUMENTS_TAB]
    with pytest.raises(StageError, match="Documents tab"):
        stages.collect(ctx, "J19")


def test_documents_tab_vote_becomes_vote_evidence(ctx: Context):
    assert isinstance(ctx.fetcher, FixtureFetcher)
    report = f"{BASE}/Reports/1/H67%20Poll%20Vote.pdf"
    ctx.fetcher.pages[DOCUMENTS_TAB] = (
        b"<table><tbody><tr><td>4/20/2025</td><td>H67 Poll Vote</td>"
        b'<td><a href="/Reports/1/H67%20Poll%20Vote.pdf">Download</a></td>'
        b"</tr></tbody></table>"
    )
    ctx.fetcher.pages[report] = b"%PDF-unreadable"
    stages.collect(ctx, "J19")
    (tab,) = ctx.store.observations.of_kind(ObservationKind.COMMITTEE_DOCUMENTS)
    stages.scrape(ctx, "J19")
    run_id = stages.evaluate(ctx, "J19")
    (h67,) = [
        e for e in ctx.store.evaluations.for_run(run_id) if e.bill.bill_id == "H67"
    ]
    votes = next(r for r in h67.results if r.requirement_id == "votes_posted")
    assert "No vote record found" not in (votes.detail or "")
    searches = ctx.store.observations.for_bill(
        194, "H67", ObservationKind.DOCUMENT_SEARCH
    )
    vote_search = next(
        o.payload
        for o in searches
        if o.extractor_id.startswith("document_search:votes")
    )
    assert vote_search.exhaustive
    assert vote_search.candidates[0].location == (
        "Committee Documents tab: H67 Poll Vote, posted 2025-04-20"
    )
    assert isinstance(tab.payload, CommitteeDocumentsPayload)
    (recorded,) = tab.payload.documents
    assert recorded.bill_ids == ("H67",) and not recorded.from_content
    assert len(tab.payload.consulted) == 1


def test_report_requires_successful_evaluation(ctx: Context):
    assert stages.report(ctx, "J19", [JsonReporter()]) == []
    stages.collect(ctx, "J19")
    with pytest.raises(StageError):
        stages.report(ctx, "J19", [JsonReporter()])


class Recording:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def emit(self, event: RunEvent) -> None:
        self.events.append(event)


def test_stages_emit_progress_and_survive_item_failures(ctx: Context):
    recording = Recording()
    observed = replace(ctx, events=recording)
    assert isinstance(ctx.fetcher, FixtureFetcher)
    stages.collect(observed, "J19")
    del ctx.fetcher.pages[f"{BASE}/Bills/194/H67"]
    run_id = stages.scrape(observed, "J19")
    assert ctx.store.runs.status(run_id) == RunStatus.SUCCEEDED
    scrape_events = [
        e
        for e in recording.events
        if isinstance(e, RunStarted | Progress | ItemFailed | RunFinished)
        and e.run_id == run_id
    ]
    started, *middle, finished = scrape_events
    assert isinstance(started, RunStarted) and started.total == 8
    progress = [e for e in middle if isinstance(e, Progress)]
    assert sorted(p.done for p in progress) == list(range(1, 9))
    failures = [e for e in middle if isinstance(e, ItemFailed)]
    assert [(f.item, f.error) for f in failures] == [("H67", "HTTP 404")]
    assert isinstance(finished, RunFinished)
    assert finished.stats["failed"] == 1


def test_vote_tab_becomes_vote_evidence(ctx: Context):
    stages.collect(ctx, "J19")
    stages.scrape(ctx, "J19")
    run_id = stages.evaluate(ctx, "J19")
    (h1768,) = [
        e for e in ctx.store.evaluations.for_run(run_id) if e.bill.bill_id == "H1768"
    ]
    votes = next(r for r in h1768.results if r.requirement_id == "votes_posted")
    assert votes.status == RequirementStatus.MET
    assert "presumed" in votes.detail
    trigger = next(s for s in votes.derivation if s.role == StepRole.TRIGGER)
    assert trigger.value == date(2025, 7, 21)


def test_reextract_refreshes_document_observations(ctx: Context, monkeypatch):
    stages.collect(ctx, "J19")
    stages.scrape(ctx, "J19")
    current = bill_tabs.FINDER_VERSION
    monkeypatch.setattr(bill_tabs, "FINDER_VERSION", current + 1)
    offline = replace(ctx, fetcher=OfflineFetcher())
    run_id = stages.reextract(offline)
    observations = ctx.store.observations.for_bill(
        194, "H1768", ObservationKind.DOCUMENT
    )
    assert {o.extractor_version for o in observations} == {current, current + 1}
    assert ctx.store.runs.status(run_id) == RunStatus.SUCCEEDED
    second = stages.reextract(offline)
    assert ctx.store.runs.status(second) == RunStatus.SUCCEEDED
    again = ctx.store.observations.for_bill(194, "H1768", ObservationKind.DOCUMENT)
    assert len(again) == len(observations)


def test_reextract_rebuilds_hearings_offline(ctx: Context, monkeypatch):
    stages.collect(ctx, "J19")
    current = hearings.EXTRACTOR_VERSION
    monkeypatch.setattr(hearings, "EXTRACTOR_VERSION", current + 1)
    run_id = stages.reextract(replace(ctx, fetcher=OfflineFetcher()))
    versions = {
        o.extractor_version
        for o in ctx.store.observations.of_kind(ObservationKind.HEARING)
    }
    assert versions == {current, current + 1}
    assert ctx.store.runs.status(run_id) == RunStatus.SUCCEEDED


def test_reextract_rebuilds_committee_documents_offline(ctx: Context, monkeypatch):
    stages.collect(ctx, "J19")
    current = committee_documents.EXTRACTOR_VERSION
    monkeypatch.setattr(committee_documents, "EXTRACTOR_VERSION", current + 1)
    run_id = stages.reextract(replace(ctx, fetcher=OfflineFetcher()))
    versions = {
        o.extractor_version
        for o in ctx.store.observations.of_kind(ObservationKind.COMMITTEE_DOCUMENTS)
    }
    assert versions == {current, current + 1}
    assert ctx.store.runs.status(run_id) == RunStatus.SUCCEEDED


def _second_night(ctx: Context, full: bool = False) -> tuple[UUID, list[str]]:
    assert isinstance(ctx.fetcher, FixtureFetcher)
    assert isinstance(ctx.clock, FixedClock)
    ctx.clock.current = AS_OF + timedelta(days=1)
    ctx.fetcher.requests.clear()
    run_id = stages.scrape(ctx, "J19", full=full)
    return run_id, list(ctx.fetcher.requests)


def test_unchanged_bills_skip_document_finders(ctx: Context):
    stages.collect(ctx, "J19")
    stages.scrape(ctx, "J19")
    run_id, requests = _second_night(ctx)
    assert not [
        u for u in requests if u.endswith(("/CommitteeVote", "/CommitteeSummary"))
    ]
    stats = ctx.store.runs.stats(run_id)
    assert stats.get("unchanged_documents_skipped", 0) > 0


def test_full_scrape_runs_document_finders(ctx: Context):
    stages.collect(ctx, "J19")
    stages.scrape(ctx, "J19")
    _, requests = _second_night(ctx, full=True)
    assert f"{BASE}/Bills/194/H1768/CommitteeVote" in requests


def test_open_obligation_runs_document_finders(ctx: Context):
    stages.collect(ctx, "J19")
    stages.scrape(ctx, "J19")
    stages.evaluate(ctx, "J19", as_of=datetime(2025, 7, 1, tzinfo=UTC))
    assert "S1043" in stages._open_bills(ctx, "J19")
    _, requests = _second_night(ctx)
    assert f"{BASE}/Bills/194/S3199/CommitteeSummary" in requests


def test_document_reasons():
    recent = AS_OF - timedelta(days=1)
    shared = stages._ScrapeShared(
        hearing_index={},
        committee_document_index={},
        tab_cache=stages._TabCache(),
        open_bills=frozenset({"H2"}),
        last_full={"H1": recent, "H2": recent, "H3": AS_OF - timedelta(days=30)},
        sweep_before=AS_OF - timedelta(days=7),
        full=False,
    )
    assert shared.document_reason("H1", page_changed=False) is None
    assert shared.document_reason("H1", page_changed=True) == "page_changed"
    assert shared.document_reason("H2", page_changed=False) == "open_obligation"
    assert shared.document_reason("H3", page_changed=False) == "sweep"
    assert shared.document_reason("H4", page_changed=False) == "sweep"
    assert replace(shared, full=True).document_reason("H1", False) == "full"


def test_all_committees_collects_the_listing_first(ctx: Context):
    assert isinstance(ctx.fetcher, FixtureFetcher)
    ctx.fetcher.pages[f"{BASE}/Committees/Joint"] = (
        b'<a href="/Committees/Detail/J19">The Judiciary</a>'
    )
    ctx.fetcher.pages[f"{BASE}/Committees/House"] = b"<p>none</p>"
    ctx.fetcher.pages[f"{BASE}/Committees/Senate"] = b"<p>none</p>"
    runner.execute(ctx, RunRequest(plan="mvp", committees=("all",)))
    (committee,) = ctx.store.inventory.committees(194)
    assert committee.committee_id == "J19"
    assert len(ctx.store.inventory.bills_for(194, "J19")) == 8
    offline = replace(ctx, fetcher=OfflineFetcher())
    runner.execute(offline, RunRequest(stage=Stage.EVALUATE, committees=("all",)))


def test_plan_reports_committee_progress(ctx: Context):
    recording = Recording()
    observed = replace(ctx, events=recording)
    assert isinstance(ctx.fetcher, FixtureFetcher)
    ctx.fetcher.pages[listing_url(BASE, 194, "J45", 1, True)] = listing_html(())
    ctx.fetcher.pages[listing_url(BASE, 194, "J45", 1, False)] = listing_html(())
    ctx.fetcher.pages[f"{BASE}/Committees/Detail/J45/Hearings"] = b"<p></p>"
    ctx.fetcher.pages[committee_documents.documents_url(BASE, "J45", 194)] = b"<p></p>"
    runner.execute(observed, RunRequest(plan="mvp", committees=("J19", "J45")))
    plan = [
        e
        for e in recording.events
        if isinstance(e, PlanStarted | CommitteeStarted | PlanFinished)
    ]
    assert isinstance(plan[0], PlanStarted) and plan[0].committees == ("J19", "J45")
    positions = [
        (e.committee_id, e.position, e.total)
        for e in plan
        if isinstance(e, CommitteeStarted)
    ]
    assert positions == [("J19", 1, 2), ("J45", 2, 2)]
    assert isinstance(plan[-1], PlanFinished) and plan[-1].committees_done == 2
    order = [
        (e.stage, e.scope)
        for e in recording.events
        if isinstance(e, RunStarted) and e.scope in ("J19", "J45")
    ]
    assert [s for s, c in order if c == "J19"] == [
        "collect",
        "scrape",
        "evaluate",
        "report",
    ]
    assert order.index(("report", "J19")) < order.index(("collect", "J45"))

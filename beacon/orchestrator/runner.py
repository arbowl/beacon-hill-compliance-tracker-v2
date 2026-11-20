"""Runs a plan or stage for a set of committees; shared by every front end."""

import logging
from collections.abc import Sequence
from datetime import date, datetime, time
from importlib.metadata import version
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx
from beacon_contract import v1
from pydantic import BaseModel, ConfigDict, model_validator

from beacon.adapters import changelog, site_push
from beacon.adapters.blobs import FileBlobStore
from beacon.adapters.clock import SystemClock
from beacon.adapters.events import (
    EventBus,
    HeartbeatSink,
    JsonLinesFileSink,
    LoggingSink,
)
from beacon.adapters.fetchers import HttpFetcher
from beacon.adapters.run_lock import RunLock
from beacon.adapters.sqlite_store import SqliteStore
from beacon.config import Settings
from beacon.domain.events import CommitteeStarted, PlanFinished, PlanStarted
from beacon.domain.local_time import LEGISLATURE_TZ
from beacon.evaluator.clauses import ClauseIndex
from beacon.orchestrator import stages
from beacon.orchestrator.stages import Context
from beacon.ports.events import EventSink
from beacon.ports.store import RunStatus, Stage
from beacon.reporters.audit_html import AuditReporter
from beacon.reporters.base import Reporter
from beacon.reporters.json_export import JsonReporter
from beacon.reporters.site_bundle import SiteBundleReporter
from beacon.reporters.v1_parity import V1ParityReporter

PLANS: dict[str, tuple[Stage, ...]] = {
    "mvp": (Stage.COLLECT, Stage.SCRAPE, Stage.REEXTRACT, Stage.EVALUATE, Stage.REPORT),
    "offline": (Stage.REEXTRACT, Stage.EVALUATE, Stage.REPORT),
}


class RunRequest(BaseModel):
    """What to run: a named plan or a single stage, and its options."""

    model_config = ConfigDict(frozen=True)

    plan: str | None = None
    stage: Stage | None = None
    committees: tuple[str, ...] = ()
    as_of: date | None = None
    full: bool = False
    v1: Path | None = None

    @model_validator(mode="after")
    def _one_target(self) -> "RunRequest":
        if (self.plan is None) == (self.stage is None):
            raise ValueError("Give exactly one of plan or stage")
        if self.plan is not None and self.plan not in PLANS:
            raise ValueError(f"Unknown plan {self.plan!r}")
        return self

    @property
    def stages(self) -> tuple[Stage, ...]:
        """The stages to run, in order."""
        if self.plan is not None:
            return PLANS[self.plan]
        assert self.stage is not None
        return (self.stage,)

    @property
    def as_of_moment(self) -> datetime | None:
        """Noon at the State House on the as-of date."""
        if self.as_of is None:
            return None
        return datetime.combine(self.as_of, time(12), LEGISLATURE_TZ)


def configure_logging(settings: Settings) -> None:
    """Send logs to data_dir/logs/beacon.log."""
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    target = str(log_dir / "beacon.log")
    if any(getattr(h, "baseFilename", None) == target for h in root.handlers):
        return
    handler = logging.FileHandler(target, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def build_context(settings: Settings, extra_sinks: Sequence[EventSink] = ()) -> Context:
    """Wire the live adapters, the standard sinks, and any front-end sinks."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    f = settings.fetch
    store = SqliteStore(settings.db_path)
    sinks: list[EventSink] = [
        JsonLinesFileSink(settings.data_dir / "events"),
        HeartbeatSink(store.runs),
        LoggingSink(),
        *extra_sinks,
    ]
    return Context(
        settings=settings,
        store=store,
        blobs=FileBlobStore(settings.blob_dir),
        fetcher=HttpFetcher(clock, f.min_interval, f.timeout, f.retries),
        clock=clock,
        clauses=ClauseIndex.load(settings.rules_dir),
        events=EventBus(sinks),
    )


def _reporters(ctx: Context, v1: Path | None) -> list[Reporter]:
    reporters: list[Reporter] = [
        JsonReporter(),
        AuditReporter(ctx.clauses),
        SiteBundleReporter(ctx.clauses),
    ]
    if v1 is not None:
        reporters.append(V1ParityReporter(v1))
    return reporters


ALL_COMMITTEES = "all"


def _scope(ctx: Context, request: RunRequest) -> tuple[str, ...]:
    committees = request.committees or ctx.settings.committees
    if ALL_COMMITTEES not in committees:
        return tuple(committees)
    if Stage.COLLECT in request.stages:
        return tuple(stages.collect_committees(ctx))
    known = ctx.store.inventory.committees(ctx.settings.session)
    return tuple(c.committee_id for c in known)


def execute(ctx: Context, request: RunRequest) -> list[Path]:
    """Run the request committee by committee; returns report files written.

    Re-extraction spans all stored data, so it runs once, first. "all" means
    every committee the site lists, refreshed when collecting.
    """
    committees = _scope(ctx, request)
    plan_id, started = uuid4(), monotonic()
    ctx.events.emit(
        PlanStarted(
            plan_id=plan_id,
            at=ctx.clock.now(),
            stages=tuple(s.value for s in request.stages),
            committees=committees,
        )
    )
    written: list[Path] = []
    done = 0
    status = RunStatus.FAILED
    try:
        if Stage.REEXTRACT in request.stages:
            stages.reextract(ctx)
        for position, committee_id in enumerate(committees, 1):
            ctx.events.emit(
                CommitteeStarted(
                    plan_id=plan_id,
                    at=ctx.clock.now(),
                    committee_id=committee_id,
                    position=position,
                    total=len(committees),
                )
            )
            written.extend(_committee(ctx, request, committee_id))
            done += 1
        status = RunStatus.SUCCEEDED
    finally:
        ctx.events.emit(
            PlanFinished(
                plan_id=plan_id,
                at=ctx.clock.now(),
                status=status.value,
                committees_done=done,
                duration_s=monotonic() - started,
            )
        )
    return written


def _committee(ctx: Context, request: RunRequest, committee_id: str) -> list[Path]:
    """Every committee-level stage of the request, in order, for one committee."""
    written: list[Path] = []
    for stage in request.stages:
        if stage == Stage.COLLECT:
            stages.collect(ctx, committee_id)
        elif stage == Stage.SCRAPE:
            stages.scrape(ctx, committee_id, full=request.full)
        elif stage == Stage.EVALUATE:
            stages.evaluate(ctx, committee_id, request.as_of_moment)
        elif stage == Stage.REPORT:
            written.extend(
                stages.report(ctx, committee_id, _reporters(ctx, request.v1))
            )
    return written


SITE_KEY_ENV = "BEACON_SITE_KEY"


class PushOutcome(BaseModel):
    """The site's replies to one push: the changelog, then the bundles."""

    model_config = ConfigDict(frozen=True)

    changelog: v1.ChangelogState
    run: v1.RunState


def push(
    settings: Settings,
    client: httpx.Client,
    committees: Sequence[str] | None = None,
) -> PushOutcome:
    """Send the changelog, then the newest bundle of each committee.

    Every committee with a bundle is sent if none are named.
    """
    try:
        notes = changelog.load(settings.site.changelog, version("beacon"))
    except changelog.ChangelogError as exc:
        raise site_push.PushError(f"{settings.site.changelog}: {exc}") from exc
    paths = site_push.latest_bundles(settings.report_dir, committees)
    bundles = [site_push.load_bundle(p) for p in paths.values()]
    if not bundles:
        raise site_push.PushError("No bundles to push; run report first")
    manifest = site_push.manifest(bundles, version("beacon"), SystemClock().now())
    pusher = site_push.SitePusher(client)
    notes_state = pusher.push_changelog(notes)
    run_state = pusher.push(manifest, {c: p.read_bytes() for c, p in paths.items()})
    return PushOutcome(changelog=notes_state, run=run_state)


def run(
    settings: Settings, request: RunRequest, extra_sinks: Sequence[EventSink] = ()
) -> list[Path]:
    """Hold the data directory's lock, build a context, and execute the request."""
    with RunLock(settings.data_dir):
        configure_logging(settings)
        ctx = build_context(settings, extra_sinks)
        try:
            return execute(ctx, request)
        finally:
            ctx.store.close()

"""The browser runner, driven through the same runner and store as the CLI."""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from beacon.adapters.blobs import FileBlobStore
from beacon.adapters.clock import FixedClock
from beacon.adapters.fetchers import FixtureFetcher
from beacon.adapters.run_lock import RunLock
from beacon.adapters.sqlite_store import SqliteStore
from beacon.config import Settings
from beacon.domain.events import (
    CommitteeStarted,
    PlanStarted,
    Progress,
    RunFinished,
    RunStarted,
)
from beacon.evaluator.clauses import ClauseIndex
from beacon.gui.app import build_app
from beacon.gui.jobs import JobProgress
from beacon.orchestrator import runner
from beacon.orchestrator.runner import RunRequest
from beacon.orchestrator.stages import Context
from beacon.ports.events import EventSink
from tests.orchestrator.test_pipeline import AS_OF, BASE, fixture_pages

RULES = Path(__file__).resolve().parents[2] / "rules"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path, base_url=BASE, committees=("J19",), rules_dir=RULES
    )


@pytest.fixture
def populated(settings: Settings) -> Settings:
    clock = FixedClock(AS_OF)
    store = SqliteStore(settings.db_path)
    ctx = Context(
        settings=settings,
        store=store,
        blobs=FileBlobStore(settings.blob_dir),
        fetcher=FixtureFetcher(clock, fixture_pages()),
        clock=clock,
        clauses=ClauseIndex.load(RULES),
    )
    runner.execute(ctx, RunRequest(plan="mvp"))
    store.close()
    return settings


class FakeRunner:
    def __init__(self) -> None:
        self.requests: list[RunRequest] = []

    def __call__(
        self, settings: Settings, request: RunRequest, sinks: Sequence[EventSink]
    ) -> list[Path]:
        self.requests.append(request)
        run_id = uuid4()
        at = datetime(2026, 11, 19, tzinfo=UTC)
        for sink in sinks:
            sink.emit(RunStarted(run_id=run_id, stage="scrape", at=at, total=1))
            sink.emit(
                RunFinished(
                    run_id=run_id,
                    stage="scrape",
                    at=at,
                    status="succeeded",
                    stats={},
                    duration_s=0.1,
                )
            )
        return [settings.data_dir / "report.json"]


def test_options_list_plans_stages_and_flags(settings: Settings):
    client = TestClient(build_app(settings, FakeRunner()))
    options = client.get("/api/options").json()
    assert options["plans"]["mvp"][0] == "collect"
    assert "evaluate" in options["stages"]
    assert set(options["flags"]) == {"as_of", "full"}
    assert any(c["id"] == "J19" for c in options["committees"])
    assert client.get("/").text.startswith("<!doctype html>")


def test_start_run_uses_shared_request(settings: Settings):
    fake = FakeRunner()
    app = build_app(settings, fake)
    client = TestClient(app)
    response = client.post(
        "/api/runs",
        json={
            "plan": "offline",
            "committees": ["J19"],
            "as_of": "2026-03-18",
            "full": True,
        },
    )
    assert response.status_code == 202
    app.state.jobs.wait(5)
    job = client.get("/api/job").json()
    assert job["status"] == "succeeded"
    (request,) = fake.requests
    assert request.plan == "offline" and request.full and request.committees == ("J19",)
    assert request.as_of_moment is not None


def test_invalid_request_is_rejected(settings: Settings):
    client = TestClient(build_app(settings, FakeRunner()))
    response = client.post("/api/runs", json={"plan": "mvp", "stage": "scrape"})
    assert response.status_code == 422


def test_busy_lock_is_reported(settings: Settings):
    client = TestClient(build_app(settings, FakeRunner()))
    with RunLock(settings.data_dir):
        response = client.post("/api/runs", json={"stage": "evaluate"})
    assert response.status_code == 409


def test_results_and_bill_detail(populated: Settings):
    client = TestClient(build_app(populated, FakeRunner()))
    results = client.get("/api/committees/J19/results").json()
    assert results["summary"] == {"Non-Compliant": 5, "Compliant": 1}
    assert results["requirement_totals"]["report_out_deadline"]["Met"] >= 1
    s1168 = next(b for b in results["bills"] if b["bill_id"] == "S1168")
    assert s1168["url"].endswith("/Bills/194/S1168")
    assert s1168["requirements"]["votes_posted"]["status"] == "Met"
    detail = client.get("/api/bills/S1168", params={"committee": "J19"}).json()
    (evaluation,) = detail["evaluations"]
    report_out = evaluation["results"][0]
    texts = [s["clause_text"] for s in report_out["derivation"] if s["clause_text"]]
    assert any("Joint Rule" in t or "joint committees" in t for t in texts)
    obs = detail["observations"][0]
    assert client.get(f"/api/observations/{obs['observation_id']}").status_code == 200
    body = client.get(f"/api/captures/{obs['capture_id']}/body")
    assert body.status_code == 200
    assert body.headers["content-security-policy"] == "sandbox"


def test_options_list_every_current_committee(populated: Settings):
    client = TestClient(build_app(populated, FakeRunner()))
    committees = client.get("/api/options").json()["committees"]
    assert len(committees) == 54
    j19 = next(c for c in committees if c["id"] == "J19")
    assert (
        j19["has_data"]
        and not next(c for c in committees if c["id"] == "J45")["has_data"]
    )


def test_overview_cards(populated: Settings):
    client = TestClient(build_app(populated, FakeRunner()))
    (card,) = client.get("/api/overview").json()
    assert card["committee_id"] == "J19" and card["bills"] == 6
    assert card["audit_url"] and card["audit_url"].endswith("/J19_audit.html")
    page = client.get(card["audit_url"])
    assert page.status_code == 200 and "Compliance audit" in page.text


def test_runs_listed(populated: Settings):
    client = TestClient(build_app(populated, FakeRunner()))
    runs = client.get("/api/runs").json()
    assert any(r["scope"] == "J19" for r in runs)
    assert {r["stage"] for r in runs} >= {"collect", "scrape", "evaluate", "report"}


def test_job_state_tracks_committee_position(settings: Settings):
    seen: list[JobProgress | None] = []

    def runner_with_plan(
        settings: Settings, request: RunRequest, sinks: Sequence[EventSink]
    ) -> list[Path]:
        at = datetime(2026, 11, 19, tzinfo=UTC)
        plan_id, run_id = uuid4(), uuid4()
        sinks[0].emit(
            PlanStarted(
                plan_id=plan_id, at=at, stages=("scrape",), committees=("J19", "J45")
            )
        )
        sinks[0].emit(
            CommitteeStarted(
                plan_id=plan_id, at=at, committee_id="J45", position=2, total=2
            )
        )
        sinks[0].emit(
            RunStarted(run_id=run_id, stage="scrape", at=at, scope="J45", total=91)
        )
        sinks[0].emit(
            Progress(
                run_id=run_id,
                stage="scrape",
                at=at,
                done=40,
                total=91,
                item="H1",
                outcome="changed",
            )
        )
        seen.append(app.state.jobs.state.progress)
        return []

    app = build_app(settings, runner_with_plan)
    TestClient(app).post(
        "/api/runs", json={"plan": "mvp", "committees": ["J19", "J45"]}
    )
    app.state.jobs.wait(5)
    (progress,) = seen
    assert progress is not None
    assert (progress.position, len(progress.committees), progress.committee_id) == (
        2,
        2,
        "J45",
    )
    assert (progress.stage, progress.done, progress.stage_total) == ("scrape", 40, 91)

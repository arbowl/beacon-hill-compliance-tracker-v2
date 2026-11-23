"""The browser runner, driven through the same runner and store as the CLI."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from beacon_contract import v1
from starlette.applications import Starlette
from starlette.testclient import TestClient

from beacon.adapters.run_lock import RunLock
from beacon.adapters.site_push import PushError
from beacon.adapters.sqlite_store import SqliteStore
from beacon.config import Settings
from beacon.domain.evaluation import NoRequirements
from beacon.domain.events import (
    CommitteeStarted,
    PlanStarted,
    Progress,
    RunFinished,
    RunStarted,
)
from beacon.domain.ids import BillKey
from beacon.gui.app import build_app
from beacon.gui.jobs import JobProgress
from beacon.orchestrator.runner import PushOutcome, RunRequest
from beacon.ports.events import EventSink
from beacon.ports.store import RunStatus, Stage

LOCAL = "http://127.0.0.1:8765"


def _client(app: Starlette) -> TestClient:
    return TestClient(app, base_url=LOCAL)


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
    client = _client(build_app(settings, FakeRunner()))
    options = client.get("/api/options").json()
    assert options["plans"]["mvp"][0] == "collect"
    assert "evaluate" in options["stages"]
    assert set(options["flags"]) == {"as_of", "full"}
    assert any(c["id"] == "J19" for c in options["committees"])
    assert client.get("/").text.startswith("<!doctype html>")


def test_start_run_uses_shared_request(settings: Settings):
    fake = FakeRunner()
    app = build_app(settings, fake)
    client = _client(app)
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
    client = _client(build_app(settings, FakeRunner()))
    response = client.post("/api/runs", json={"plan": "mvp", "stage": "scrape"})
    assert response.status_code == 422


def test_busy_lock_is_reported(settings: Settings):
    client = _client(build_app(settings, FakeRunner()))
    with RunLock(settings.data_dir):
        response = client.post("/api/runs", json={"stage": "evaluate"})
    assert response.status_code == 409


def test_results_and_bill_detail(populated: Settings):
    client = _client(build_app(populated, FakeRunner()))
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


def test_referrals_owing_nothing_are_shown_apart(populated: Settings):
    store = SqliteStore(populated.db_path)
    run_id = store.runs.start(Stage.EVALUATE, "0", "cfg")
    store.evaluations.append_no_requirements(
        [
            NoRequirements(
                run_id=run_id,
                bill=BillKey(194, "S1168"),
                committee_id="J19",
                tenure_id="194/S1168:J19:2025-09-01",
                referred_on=date(2025, 9, 1),
                ruleset_id="s194",
                ruleset_version="9",
                as_of=datetime(2026, 11, 20, tzinfo=UTC),
                awaiting=("votes_posted",),
            )
        ]
    )
    store.runs.finish(run_id, RunStatus.SUCCEEDED, {})
    store.close()
    client = _client(build_app(populated, FakeRunner()))
    (card,) = client.get("/api/overview").json()
    assert card["summary"] == {} and card["no_requirements"] == {"none_yet": 1}
    results = client.get("/api/committees/J19/results").json()
    assert results["summary"] == {} and results["no_requirements"] == {"none_yet": 1}
    (row,) = results["bills"]
    assert row["label"] is None and row["no_requirements"]["awaiting"] == [
        "votes_posted"
    ]
    detail = client.get("/api/bills/S1168", params={"committee": "J19"}).json()
    assert detail["evaluations"] == []
    assert [n["reason"] for n in detail["no_requirements"]] == ["none_yet"]


def test_options_list_every_current_committee(populated: Settings):
    client = _client(build_app(populated, FakeRunner()))
    committees = client.get("/api/options").json()["committees"]
    assert len(committees) == 54
    j19 = next(c for c in committees if c["id"] == "J19")
    assert (
        j19["has_data"]
        and not next(c for c in committees if c["id"] == "J45")["has_data"]
    )


def test_overview_cards(populated: Settings):
    client = _client(build_app(populated, FakeRunner()))
    (card,) = client.get("/api/overview").json()
    assert card["committee_id"] == "J19" and card["bills"] == 6
    assert card["audit_url"] and card["audit_url"].endswith("/J19_audit.html")
    page = client.get(card["audit_url"])
    assert page.status_code == 200 and "Compliance audit" in page.text


def test_runs_listed(populated: Settings):
    client = _client(build_app(populated, FakeRunner()))
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
    _client(app).post("/api/runs", json={"plan": "mvp", "committees": ["J19", "J45"]})
    app.state.jobs.wait(5)
    (progress,) = seen
    assert progress is not None
    assert (progress.position, len(progress.committees), progress.committee_id) == (
        2,
        2,
        "J45",
    )
    assert (progress.stage, progress.done, progress.stage_total) == ("scrape", 40, 91)


class FakePusher:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[Sequence[str] | None] = []
        self.error = error

    def __call__(self, committees: Sequence[str] | None) -> PushOutcome:
        self.calls.append(committees)
        if self.error is not None:
            raise self.error
        at = datetime(2026, 11, 19, tzinfo=UTC)
        return PushOutcome(
            changelog=v1.ChangelogState(releases=("0.1.0",), updated=()),
            run=v1.RunState(
                push_id=uuid4(),
                status=v1.RunStatus.PUBLISHED,
                committees=tuple(committees or ("J19",)),
                uploaded=tuple(committees or ("J19",)),
                opened_at=at,
                published_at=at,
            ),
        )


def _bundle_runner(
    settings: Settings, request: RunRequest, sinks: Sequence[EventSink]
) -> list[Path]:
    return [settings.data_dir / "2026-11-19" / "J19.bundle.json.gz"]


def _app(settings: Settings, pusher: FakePusher, run=_bundle_runner) -> Starlette:
    return build_app(settings, run, pusher, environ={})


def test_target_saved_without_ever_returning_the_key(settings: Settings):
    client = _client(_app(settings, FakePusher()))
    control = client.get("/api/control").json()
    assert control["target"]["url"] is None and control["target"]["key_source"] is None
    saved = client.put(
        "/api/target",
        json={"url": "https://tracker.example.org", "key": "sekrit", "auto": True},
    )
    assert saved.status_code == 200
    target = saved.json()["target"]
    assert target["url"] == "https://tracker.example.org"
    assert (target["url_source"], target["key_source"], target["auto"]) == (
        "saved",
        "saved",
        True,
    )
    assert "sekrit" not in saved.text
    assert "sekrit" not in client.get("/api/control").text
    bad = client.put("/api/target", json={"url": "http://tracker.example.org"})
    assert bad.status_code == 422 and "https" in bad.json()["error"]


def test_schedule_saved_and_next_run_reported(settings: Settings):
    client = _client(_app(settings, FakePusher()))
    response = client.put(
        "/api/schedule",
        json={"enabled": True, "at": "01:00", "days": [0, 2, 4], "plan": "mvp"},
    )
    assert response.status_code == 200
    control = response.json()
    assert control["schedule"]["days"] == [0, 2, 4] and control["next_run"]
    assert client.app.state.scheduler.next_due is not None
    empty = client.put("/api/schedule", json={"enabled": True, "days": []})
    assert (
        empty.status_code == 422 and empty.json()["error"] == "Choose at least one day"
    )


def test_cross_origin_and_foreign_host_refused(settings: Settings):
    client = _client(_app(settings, FakePusher()))
    evil = {"Origin": "https://evil.example"}
    for method, path in [
        ("PUT", "/api/target"),
        ("PUT", "/api/schedule"),
        ("POST", "/api/push"),
        ("POST", "/api/runs"),
    ]:
        assert client.request(method, path, json={}, headers=evil).status_code == 403
    same = client.put("/api/target", json={}, headers={"Origin": LOCAL})
    assert same.status_code == 200
    rebound = TestClient(client.app, base_url="http://evil.example:8765")
    assert rebound.get("/api/control").status_code == 400


def test_send_now_pushes_every_committee(settings: Settings):
    pusher = FakePusher()
    app = _app(settings, pusher)
    response = _client(app).post("/api/push")
    assert response.status_code == 202
    app.state.jobs.wait(5)
    job = _client(app).get("/api/job").json()
    assert (
        job["status"] == "succeeded" and job["pushed"]["run"]["status"] == "published"
    )
    assert pusher.calls == [None]


def test_auto_push_sends_what_the_run_wrote(settings: Settings):
    pusher = FakePusher()
    app = _app(settings, pusher)
    client = _client(app)
    client.put("/api/target", json={"url": "https://a.example", "auto": True})
    client.post("/api/runs", json={"plan": "offline"})
    app.state.jobs.wait(5)
    assert pusher.calls == [["J19"]]
    client.post("/api/runs", json={"plan": "offline", "as_of": "2026-03-18"})
    app.state.jobs.wait(5)
    assert pusher.calls == [["J19"]]
    assert client.get("/api/job").json()["pushed"] is None


def test_auto_push_skipped_when_run_writes_no_bundles(settings: Settings):
    pusher = FakePusher()
    app = _app(settings, pusher, run=FakeRunner())
    client = _client(app)
    client.put("/api/target", json={"url": "https://a.example", "auto": True})
    client.post("/api/runs", json={"plan": "offline"})
    app.state.jobs.wait(5)
    assert pusher.calls == [] and client.get("/api/job").json()["status"] == "succeeded"


def test_push_failure_is_reported_as_the_push_phase(settings: Settings):
    pusher = FakePusher(PushError("HTTP 401 unauthorized: bad key"))
    app = _app(settings, pusher)
    _client(app).post("/api/push")
    app.state.jobs.wait(5)
    job = _client(app).get("/api/job").json()
    assert (job["status"], job["phase"]) == ("failed", "push")
    assert job["error"] == "HTTP 401 unauthorized: bad key"

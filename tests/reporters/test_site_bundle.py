"""The site bundle carries every evaluation with provenance and pushes idempotently."""

import gzip
import json
from itertools import product
from pathlib import Path

import httpx
import pytest
from beacon_contract import v1

from beacon.adapters.site_push import (
    INGEST_PATH,
    PushError,
    SitePusher,
    latest_bundles,
    load_bundle,
    manifest,
    push_id,
)
from beacon.domain.evaluation import (
    RequirementResult,
    RequirementStatus,
    default_roll_up,
)
from beacon.evaluator.clauses import ClauseIndex
from beacon.orchestrator import stages
from beacon.orchestrator.stages import Context
from beacon.reporters.site_bundle import BundleError, SiteBundleReporter
from tests.orchestrator.test_pipeline import AS_OF, ctx  # noqa: F401


def _bundle_path(context: Context, reporter: SiteBundleReporter | None = None) -> Path:
    stages.collect(context, "J19")
    stages.scrape(context, "J19")
    stages.evaluate(context, "J19")
    (path,) = stages.report(
        context, "J19", [reporter or SiteBundleReporter(context.clauses)]
    )
    return path


def test_bundle_mirrors_evaluations(ctx: Context):  # noqa: F811
    path = _bundle_path(ctx)
    assert path.name == "J19.bundle.json.gz"
    bundle = load_bundle(path)
    run_id = ctx.store.evaluations.latest_run(194, "J19", "s194")
    evaluations = ctx.store.evaluations.for_run(run_id)
    assert bundle.evaluation_run_id == run_id
    assert {t.tenure_id: t.label.value for t in bundle.tenures} == {
        e.tenure_id: e.label.value for e in evaluations
    }
    assert bundle.committee.url.path == "/Committees/Detail/J19"
    evidence = [e for t in bundle.tenures for r in t.requirements for e in r.evidence]
    assert evidence and all(e.line is not None for e in evidence)
    cited = {
        c.text_hash for t in bundle.tenures for r in t.requirements for c in r.clauses
    }
    assert cited == {c.text_hash for c in bundle.clauses}


def test_changed_rule_text_refuses_bundle(ctx: Context):  # noqa: F811
    stages.collect(ctx, "J19")
    stages.scrape(ctx, "J19")
    stages.evaluate(ctx, "J19")
    edited = ClauseIndex(
        {
            source: {
                rule: tuple(p + " (edited)" for p in paras) for rule, paras in t.items()
            }
            for source, t in ctx.clauses.rules.items()
        }
    )
    with pytest.raises(BundleError, match="changed since evaluation"):
        stages.report(ctx, "J19", [SiteBundleReporter(edited)])


def test_latest_bundles_picks_newest_date(tmp_path: Path):
    for stamp in ("2026-11-18", "2026-11-20", "2026-11-19"):
        (tmp_path / stamp).mkdir()
        (tmp_path / stamp / "J19.bundle.json.gz").write_bytes(b"")
    assert latest_bundles(tmp_path)["J19"].parent.name == "2026-11-20"
    with pytest.raises(PushError, match="J45"):
        latest_bundles(tmp_path, ["J45"])


class FakeSite:
    """Records ingest calls and answers like the site."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.status = v1.RunStatus.OPEN
        self.uploaded: list[str] = []
        self.committees: tuple[str, ...] = ()

    def _state(self, push: str) -> httpx.Response:
        state = v1.RunState(
            push_id=push,
            status=self.status,
            committees=self.committees,
            uploaded=tuple(self.uploaded),
            opened_at=AS_OF,
            published_at=AS_OF if self.status == v1.RunStatus.PUBLISHED else None,
        )
        return httpx.Response(200, content=state.model_dump_json())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer k"
        if request.method == "POST" and request.url.path == INGEST_PATH:
            body = v1.RunManifest.model_validate_json(request.content)
            self.committees = body.committees
            return self._state(str(body.push_id))
        push = request.url.path.split("/")[5]
        if request.method == "PUT":
            assert request.headers["Content-Encoding"] == "gzip"
            bundle = v1.CommitteeBundle.model_validate_json(
                gzip.decompress(request.content)
            )
            self.uploaded.append(bundle.committee.committee_id)
            return self._state(push)
        self.status = v1.RunStatus.PUBLISHED
        return self._state(push)


def _client(site: FakeSite) -> httpx.Client:
    return httpx.Client(
        base_url="https://site.test",
        headers={"Authorization": "Bearer k"},
        transport=httpx.MockTransport(site),
    )


def test_push_uploads_then_publishes_and_repeat_is_noop(ctx: Context):  # noqa: F811
    path = _bundle_path(ctx)
    bundle = load_bundle(path)
    m = manifest([bundle], "test", AS_OF)
    assert m.push_id == push_id([bundle])
    site = FakeSite()
    state = SitePusher(_client(site)).push(m, {"J19": path.read_bytes()})
    assert state.status == v1.RunStatus.PUBLISHED
    assert [c[0] for c in site.calls] == ["POST", "PUT", "POST"]
    site.calls.clear()
    SitePusher(_client(site)).push(m, {"J19": path.read_bytes()})
    assert site.calls == [("POST", INGEST_PATH)]


def test_push_reports_site_errors(ctx: Context):  # noqa: F811
    path = _bundle_path(ctx)
    error = {
        "error": {
            "code": "invalid_bundle",
            "message": "Bundle failed validation",
            "details": [{"loc": ["tenures", 0], "msg": "label does not roll up"}],
        }
    }
    transport = httpx.MockTransport(
        lambda r: httpx.Response(422, content=json.dumps(error))
    )
    client = httpx.Client(base_url="https://site.test", transport=transport)
    with pytest.raises(PushError, match=r"422 invalid_bundle.*tenures\.0: label"):
        SitePusher(client).push(manifest([load_bundle(path)], "t", AS_OF), {})


@pytest.mark.parametrize("statuses", list(product(RequirementStatus, repeat=3)))
def test_roll_up_matches_contract(statuses: tuple[RequirementStatus, ...]):
    results = [
        RequirementResult(
            requirement_id="r",
            obligation_id=f"o{i}",
            status=s,
            reason="missing_data" if s == RequirementStatus.PROVISIONAL else None,
            detail="d",
        )
        for i, s in enumerate(statuses)
    ]
    contract = v1.roll_up(v1.RequirementStatus(s.value) for s in statuses)
    assert default_roll_up(results).value == contract.value

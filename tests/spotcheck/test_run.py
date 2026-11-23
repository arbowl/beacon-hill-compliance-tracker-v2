"""Drawing samples from a real pipeline run and recording verdicts on them."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from beacon.adapters.blobs import FileBlobStore
from beacon.adapters.clock import FixedClock
from beacon.adapters.fetchers import FixtureFetcher
from beacon.adapters.sqlite_store import SqliteStore
from beacon.config import Settings
from beacon.domain.observations import ActionType
from beacon.evaluator.clauses import ClauseIndex
from beacon.evaluator.rulesets.s194.ruleset import RULESET
from beacon.orchestrator import stages
from beacon.orchestrator.stages import Context
from beacon.spotcheck import ledger
from beacon.spotcheck.records import ItemVerdict, Manifest, Verdict
from beacon.spotcheck.run import (
    MANIFEST,
    RULES,
    VERDICTS,
    SampleRequest,
    Sources,
    SpotcheckError,
    record,
    sample,
)
from tests.orchestrator.test_pipeline import AS_OF, BASE, fixture_pages


@pytest.fixture(scope="module")
def evaluated(tmp_path_factory: pytest.TempPathFactory, clause_index: ClauseIndex):
    root = tmp_path_factory.mktemp("pipeline")
    settings = Settings(data_dir=root, base_url=BASE, committees=("J19",))
    clock = FixedClock(AS_OF)
    store = SqliteStore(settings.db_path)
    ctx = Context(
        settings=settings,
        store=store,
        blobs=FileBlobStore(settings.blob_dir),
        fetcher=FixtureFetcher(clock, fixture_pages()),
        clock=clock,
        clauses=clause_index,
    )
    stages.collect(ctx, "J19")
    stages.scrape(ctx, "J19")
    stages.evaluate(ctx, "J19")
    yield ctx
    store.close()


def _sources(ctx: Context) -> Sources:
    return Sources(
        store=ctx.store,
        blobs=ctx.blobs,
        clauses=ctx.clauses,
        ruleset=RULESET,
        clock=FixedClock(AS_OF),
        session=194,
        code_version="test",
    )


def _manifest(directory: Path) -> Manifest:
    return Manifest.model_validate_json((directory / MANIFEST).read_text("utf-8"))


def test_sample_writes_blind_dossiers_keys_and_rules(evaluated: Context, tmp_path):
    directory = sample(
        _sources(evaluated), SampleRequest(count=3, seed=1), tmp_path, tmp_path / "l"
    )
    manifest = _manifest(directory)
    assert [i.item for i in manifest.items] == [1, 2, 3]
    assert manifest.frame_size == 6
    assert manifest.ruleset_version == RULESET.version
    assert "Joint Rule 10" in (directory / RULES).read_text("utf-8")
    for item in manifest.items:
        dossier = (directory / item.dossier).read_text("utf-8")
        key = (directory / item.key).read_text("utf-8")
        assert "Referred to the committee" in dossier
        assert ActionType.REFERRED.value not in dossier
        assert item.label.value not in dossier
        assert "Met" not in dossier
        assert f"## Evaluation by current code (s194 v{RULESET.version})" in key
        assert ActionType.REFERRED.value in key


def test_same_seed_draws_the_same_tenures(evaluated: Context, tmp_path):
    first = sample(
        _sources(evaluated),
        SampleRequest(count=4, seed=9),
        tmp_path / "a",
        tmp_path / "l",
    )
    second = sample(
        _sources(evaluated),
        SampleRequest(count=4, seed=9),
        tmp_path / "b",
        tmp_path / "l",
    )
    tenures = [[i.tenure_id for i in _manifest(d).items] for d in (first, second)]
    assert tenures[0] == tenures[1]


def _write_verdicts(directory: Path, items: list[dict]) -> None:
    sheet = {"auditor": "test", "items": items}
    (directory / VERDICTS).write_text(json.dumps(sheet), encoding="utf-8")


def test_recorded_tenures_are_not_drawn_again(evaluated: Context, tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    sources = _sources(evaluated)
    directory = sample(sources, SampleRequest(count=6, seed=2), tmp_path, ledger_path)
    manifest = _manifest(directory)
    requirement = manifest.items[1].results[0].requirement_id
    _write_verdicts(
        directory,
        [{"item": i.item, "verdict": "agree"} for i in manifest.items[:1]]
        + [
            {
                "item": i.item,
                "verdict": "disagree",
                "findings": [
                    {
                        "category": "rule_logic",
                        "requirement_id": requirement,
                        "note": "x",
                    }
                ],
            }
            for i in manifest.items[1:2]
        ]
        + [
            {
                "item": i.item,
                "verdict": "uncertain",
                "findings": [{"category": "data_gap", "note": "y"}],
            }
            for i in manifest.items[2:]
        ],
    )
    entries = record(directory, ledger_path, FixedClock(AS_OF))
    assert [e.tenure_id for e in entries] == [i.tenure_id for i in manifest.items]
    assert ledger.read(ledger_path) == entries
    assert "1/6 agree" in ledger.summary(entries)
    with pytest.raises(ledger.LedgerError, match="already recorded"):
        record(directory, ledger_path, FixedClock(AS_OF))
    with pytest.raises(SpotcheckError, match="no unaudited"):
        sample(sources, SampleRequest(count=1, seed=3), tmp_path / "c", ledger_path)


def test_findings_must_name_a_requirement_the_item_was_evaluated_against(
    evaluated: Context, tmp_path
):
    directory = sample(
        _sources(evaluated), SampleRequest(count=1, seed=4), tmp_path, tmp_path / "l"
    )
    _write_verdicts(
        directory,
        [
            {
                "item": 1,
                "verdict": "disagree",
                "findings": [
                    {"category": "tenure", "requirement_id": "nope", "note": "z"}
                ],
            }
        ],
    )
    with pytest.raises(ledger.LedgerError, match="not evaluated against nope"):
        record(directory, tmp_path / "l", FixedClock(AS_OF))
    _write_verdicts(directory, [{"item": 7, "verdict": "agree"}])
    with pytest.raises(ledger.LedgerError, match="item 7"):
        record(directory, tmp_path / "l", FixedClock(AS_OF))


@pytest.mark.parametrize(
    "spec",
    [
        {
            "item": 1,
            "verdict": "agree",
            "findings": [{"category": "tenure", "note": "n"}],
        },
        {"item": 1, "verdict": "disagree"},
        {
            "item": 1,
            "verdict": "uncertain",
            "findings": [{"category": "bogus", "note": "n"}],
        },
    ],
)
def test_verdicts_require_findings_unless_agreeing(spec: dict):
    with pytest.raises(ValidationError):
        ItemVerdict.model_validate(spec)


def test_agree_needs_no_findings():
    assert ItemVerdict.model_validate({"item": 1, "verdict": "agree"}).verdict == (
        Verdict.AGREE
    )

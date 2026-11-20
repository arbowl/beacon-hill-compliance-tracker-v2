"""Settings over a temporary data directory, empty or after a fixture run."""

from pathlib import Path

import pytest

from beacon.adapters.blobs import FileBlobStore
from beacon.adapters.clock import FixedClock
from beacon.adapters.fetchers import FixtureFetcher
from beacon.adapters.sqlite_store import SqliteStore
from beacon.config import Settings
from beacon.evaluator.clauses import ClauseIndex
from beacon.orchestrator import runner
from beacon.orchestrator.runner import RunRequest
from beacon.orchestrator.stages import Context
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

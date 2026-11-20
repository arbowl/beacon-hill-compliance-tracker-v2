"""Tests for the SQLite store and the blob store."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from beacon.adapters.blobs import FileBlobStore
from beacon.adapters.sqlite_store import SqliteStore
from beacon.domain.captures import Capture
from beacon.domain.ids import BillKey, Chamber
from beacon.domain.inventory import BillRecord, CommitteeRecord
from beacon.domain.observations import ActionType as A
from beacon.domain.observations import ObservationKind
from beacon.evaluator.clauses import ClauseIndex
from beacon.evaluator.engine import evaluate
from beacon.evaluator.facts import project
from beacon.evaluator.rulesets.s194.ruleset import RULESET
from beacon.ports.store import RunStatus, Stage
from tests.builders import history, row

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path):
    s = SqliteStore(tmp_path / "beacon.db")
    yield s
    s.close()


def capture(url: str, at: datetime, status: int = 200, **kw) -> Capture:
    return Capture(
        capture_id=uuid4(),
        run_id=uuid4(),
        url=url,
        requested_at=at,
        http_status=status,
        content_hash=kw.pop("content_hash", "a" * 64 if status in (200, 304) else None),
        **kw,
    )


class TestBlobs:
    def test_round_trip_and_idempotence(self, tmp_path: Path):
        blobs = FileBlobStore(tmp_path / "blobs")
        h = blobs.put(b"<html>")
        assert blobs.put(b"<html>") == h
        assert blobs.get(h) == b"<html>"
        assert blobs.exists(h)
        path = tmp_path / "blobs" / h[:2] / h[2:4] / h
        assert path.read_bytes()[:2] == bytes((0x1F, 0x8B))

    def test_reads_legacy_uncompressed(self, tmp_path: Path):
        blobs = FileBlobStore(tmp_path / "blobs")
        h = blobs.put(b"x")
        (tmp_path / "blobs" / h[:2] / h[2:4] / h).write_bytes(b"x")
        assert blobs.get(h) == b"x"


class TestRuns:
    def test_lifecycle(self, store: SqliteStore):
        run_id = store.runs.start(Stage.SCRAPE, "0.1.0", "cfg")
        assert store.runs.status(run_id) == RunStatus.RUNNING
        store.runs.finish(run_id, RunStatus.SUCCEEDED, {"captures": 3})
        assert store.runs.status(run_id) == RunStatus.SUCCEEDED

    def test_unknown_run(self, store: SqliteStore):
        with pytest.raises(KeyError):
            store.runs.status(uuid4())


class TestCaptures:
    def test_latest_for_includes_304_and_skips_errors(self, store: SqliteStore):
        url = "https://malegislature.gov/Bills/194/H1"
        first = capture(url, T0)
        confirm = capture(
            url, T0 + timedelta(days=1), 304, not_modified_of=first.capture_id
        )
        failed = capture(url, T0 + timedelta(days=2), 503)
        for c in (first, confirm, failed):
            store.captures.add(c)
        assert store.captures.latest_for(url) == confirm
        assert store.captures.get(first.capture_id) == first

    def test_iter_with_content_by_prefix(self, store: SqliteStore):
        a = capture("https://x/Bills/194/H1", T0)
        b = capture("https://x/Committees/J19", T0)
        c = capture("https://x/Bills/194/H2", T0 + timedelta(hours=1), 503)
        for item in (a, b, c):
            store.captures.add(item)
        assert list(store.captures.iter_with_content("https://x/Bills/")) == [a]

    def test_timezone_preserved(self, store: SqliteStore):
        local = datetime(2026, 1, 1, 9, tzinfo=ZoneInfo("America/New_York"))
        c = capture("https://x", local)
        store.captures.add(c)
        assert store.captures.get(c.capture_id).requested_at == local


class TestInventory:
    def test_bills_for_committee(self, store: SqliteStore):
        store.inventory.upsert_committee(
            CommitteeRecord(
                session=194,
                committee_id="J19",
                name="Joint Committee on the Judiciary",
                chamber=Chamber.JOINT,
                url="https://x/Committees/Detail/J19",
            )
        )
        for bill_id in ("H2", "H1"):
            store.inventory.upsert_bill(
                BillRecord(session=194, bill_id=bill_id, url=f"https://x/{bill_id}")
            )
            store.inventory.link(194, "J19", bill_id, T0)
        store.inventory.link(194, "J19", "H1", T0 + timedelta(days=1))
        store.inventory.upsert_bill(
            BillRecord(session=194, bill_id="H1", url="https://x/H1", title="An Act")
        )
        bills = store.inventory.bills_for(194, "J19")
        assert [b.bill_id for b in bills] == ["H1", "H2"]
        assert bills[0].title == "An Act"
        committee = store.inventory.committee(194, "J19")
        assert committee is not None and committee.chamber == Chamber.JOINT
        assert store.inventory.committee(194, "J99") is None


class TestObservations:
    def test_round_trip_and_uniqueness(self, store: SqliteStore):
        obs = history("H1", [row("2025-02-01", A.REFERRED, "J19")])
        store.observations.append_many([obs])
        assert store.observations.for_bill(194, "H1") == [obs]
        assert store.observations.for_bill(194, "H1", ObservationKind.BILL_HISTORY)
        assert store.observations.exists(obs.capture_id, "bill_history", 1)
        assert not store.observations.exists(obs.capture_id, "bill_history", 2)
        with pytest.raises(Exception, match="UNIQUE"):
            store.observations.append_many(
                [obs.model_copy(update={"observation_id": uuid4()})]
            )


class TestEvaluations:
    def test_round_trip_and_latest_run(
        self, store: SqliteStore, clause_index: ClauseIndex
    ):
        facts = project(
            BillKey(194, "H1"),
            [history("H1", [row("2025-02-01", A.REFERRED, "J19")])],
        )
        run_id = store.runs.start(Stage.EVALUATE, "0.1.0", "cfg")
        as_of = datetime(2025, 6, 1, 12, tzinfo=ZoneInfo("America/New_York"))
        evaluations = evaluate(facts, "J19", RULESET, clause_index, as_of, run_id)
        store.evaluations.append_many(evaluations)
        assert store.evaluations.for_run(run_id) == evaluations
        assert store.evaluations.latest_run(194, "J19", "s194") is None
        store.runs.finish(run_id, RunStatus.SUCCEEDED, {})
        assert store.evaluations.latest_run(194, "J19", "s194") == run_id


class TestReads:
    def test_recent_runs_and_observation_get(self, store: SqliteStore):
        first = store.runs.start(Stage.COLLECT, "0", "cfg")
        second = store.runs.start(Stage.SCRAPE, "0", "cfg")
        store.runs.heartbeat(second, {"done": 3})
        store.runs.finish(first, RunStatus.SUCCEEDED, {"bills": 2})
        recent = store.runs.recent()
        assert [r.run_id for r in recent] == [second, first]
        assert recent[0].progress == {"done": 3} and recent[1].stats == {"bills": 2}
        obs = history("H1", [row("2025-02-01", A.REFERRED, "J19")])
        store.observations.append_many([obs])
        assert store.observations.get(obs.observation_id) == obs
        with pytest.raises(KeyError):
            store.observations.get(uuid4())

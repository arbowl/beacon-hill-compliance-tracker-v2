"""SQLite implementation of the Store port."""

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from beacon.domain.captures import Capture
from beacon.domain.evaluation import TenureEvaluation
from beacon.domain.inventory import BillRecord, CommitteeRecord
from beacon.domain.observations import Observation, ObservationKind
from beacon.ports.store import RunRecord, RunStatus, Stage

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    code_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    stats_json TEXT,
    progress_json TEXT,
    heartbeat_at TEXT,
    scope TEXT
);
CREATE TABLE IF NOT EXISTS captures (
    capture_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    url TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    content_hash TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS captures_url ON captures (url, requested_at);
CREATE TABLE IF NOT EXISTS committees (
    session INTEGER NOT NULL,
    committee_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (session, committee_id)
);
CREATE TABLE IF NOT EXISTS bills (
    session INTEGER NOT NULL,
    bill_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (session, bill_id)
);
CREATE TABLE IF NOT EXISTS committee_bills (
    session INTEGER NOT NULL,
    committee_id TEXT NOT NULL,
    bill_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (session, committee_id, bill_id)
);
CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    capture_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    extractor_id TEXT NOT NULL,
    extractor_version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    session INTEGER NOT NULL,
    bill_id TEXT,
    committee_id TEXT,
    payload_json TEXT NOT NULL,
    UNIQUE (capture_id, extractor_id, extractor_version)
);
CREATE INDEX IF NOT EXISTS observations_bill ON observations (session, bill_id, kind);
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    session INTEGER NOT NULL,
    bill_id TEXT NOT NULL,
    committee_id TEXT NOT NULL,
    tenure_id TEXT NOT NULL,
    ruleset_id TEXT NOT NULL,
    ruleset_version TEXT NOT NULL,
    as_of TEXT NOT NULL,
    label TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evaluations_run ON evaluations (run_id);
CREATE INDEX IF NOT EXISTS evaluations_committee
    ON evaluations (session, committee_id, ruleset_id, as_of);
CREATE TABLE IF NOT EXISTS requirement_results (
    evaluation_id TEXT NOT NULL,
    obligation_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    due TEXT,
    resolves_on TEXT,
    completed_on TEXT,
    PRIMARY KEY (evaluation_id, obligation_id)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _Db:
    def __init__(self, path: Path | str) -> None:
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(runs)")}
        for column in ("progress_json", "heartbeat_at", "scope"):
            if column not in columns:
                self.conn.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")


class SqliteRuns:
    def __init__(self, db: _Db) -> None:
        self._db = db

    def start(
        self,
        stage: Stage,
        code_version: str,
        config_hash: str,
        scope: str | None = None,
    ) -> UUID:
        run_id = uuid4()
        with self._db.lock, self._db.conn:
            self._db.conn.execute(
                "INSERT INTO runs (run_id, stage, started_at, code_version, "
                "config_hash, status, scope) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(run_id),
                    stage.value,
                    _now(),
                    code_version,
                    config_hash,
                    RunStatus.RUNNING.value,
                    scope,
                ),
            )
        return run_id

    def finish(self, run_id: UUID, status: RunStatus, stats: dict[str, int]) -> None:
        with self._db.lock, self._db.conn:
            self._db.conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, stats_json = ? "
                "WHERE run_id = ?",
                (_now(), status.value, json.dumps(stats), str(run_id)),
            )

    def heartbeat(self, run_id: UUID, progress: dict[str, int]) -> None:
        with self._db.lock, self._db.conn:
            self._db.conn.execute(
                "UPDATE runs SET progress_json = ?, heartbeat_at = ? WHERE run_id = ?",
                (json.dumps(progress), _now(), str(run_id)),
            )

    def progress(self, run_id: UUID) -> tuple[dict[str, int], datetime | None]:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT progress_json, heartbeat_at FROM runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
        if row is None:
            raise KeyError(str(run_id))
        progress = json.loads(row[0]) if row[0] else {}
        return progress, datetime.fromisoformat(row[1]) if row[1] else None

    def recent(self, limit: int = 50) -> list[RunRecord]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT run_id, stage, status, started_at, finished_at, "
                "heartbeat_at, stats_json, progress_json, scope FROM runs "
                "ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            RunRecord(
                run_id=UUID(r[0]),
                stage=Stage(r[1]),
                status=RunStatus(r[2]),
                started_at=datetime.fromisoformat(r[3]),
                finished_at=datetime.fromisoformat(r[4]) if r[4] else None,
                heartbeat_at=datetime.fromisoformat(r[5]) if r[5] else None,
                stats=json.loads(r[6]) if r[6] else {},
                progress=json.loads(r[7]) if r[7] else {},
                scope=r[8],
            )
            for r in rows
        ]

    def stats(self, run_id: UUID) -> dict[str, int]:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT stats_json FROM runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
        if row is None:
            raise KeyError(str(run_id))
        return json.loads(row[0]) if row[0] else {}

    def status(self, run_id: UUID) -> RunStatus:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
        if row is None:
            raise KeyError(str(run_id))
        return RunStatus(row[0])


class SqliteCaptures:
    def __init__(self, db: _Db) -> None:
        self._db = db

    def add(self, capture: Capture) -> None:
        with self._db.lock, self._db.conn:
            self._db.conn.execute(
                "INSERT INTO captures VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(capture.capture_id),
                    str(capture.run_id),
                    capture.url,
                    capture.requested_at.isoformat(),
                    capture.http_status,
                    capture.content_hash,
                    capture.model_dump_json(),
                ),
            )

    def get(self, capture_id: UUID) -> Capture:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT payload_json FROM captures WHERE capture_id = ?",
                (str(capture_id),),
            ).fetchone()
        if row is None:
            raise KeyError(str(capture_id))
        return Capture.model_validate_json(row[0])

    def latest_for(self, url: str) -> Capture | None:
        """Latest capture of the URL that has content (a 200, or a 304)."""
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT payload_json FROM captures "
                "WHERE url = ? AND content_hash IS NOT NULL "
                "ORDER BY requested_at DESC LIMIT 1",
                (url,),
            ).fetchone()
        return Capture.model_validate_json(row[0]) if row else None

    def iter_with_content(self, url_prefix: str = "") -> Iterator[Capture]:
        """Captures with content (200 or 304) under the URL prefix, oldest first."""
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT payload_json FROM captures "
                "WHERE content_hash IS NOT NULL AND substr(url, 1, ?) = ? "
                "ORDER BY requested_at",
                (len(url_prefix), url_prefix),
            ).fetchall()
        for (payload,) in rows:
            yield Capture.model_validate_json(payload)


class SqliteInventory:
    def __init__(self, db: _Db) -> None:
        self._db = db

    def upsert_committee(self, committee: CommitteeRecord) -> None:
        with self._db.lock, self._db.conn:
            self._db.conn.execute(
                "INSERT INTO committees VALUES (?, ?, ?) "
                "ON CONFLICT DO UPDATE SET payload_json = excluded.payload_json",
                (
                    committee.session,
                    committee.committee_id,
                    committee.model_dump_json(),
                ),
            )

    def upsert_bill(self, bill: BillRecord) -> None:
        with self._db.lock, self._db.conn:
            self._db.conn.execute(
                "INSERT INTO bills VALUES (?, ?, ?) "
                "ON CONFLICT DO UPDATE SET payload_json = excluded.payload_json",
                (bill.session, bill.bill_id, bill.model_dump_json()),
            )

    def link(
        self, session: int, committee_id: str, bill_id: str, seen_at: datetime
    ) -> None:
        stamp = seen_at.isoformat()
        with self._db.lock, self._db.conn:
            self._db.conn.execute(
                "INSERT INTO committee_bills VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT DO UPDATE SET last_seen_at = excluded.last_seen_at",
                (session, committee_id, bill_id, stamp, stamp),
            )

    def committee(self, session: int, committee_id: str) -> CommitteeRecord | None:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT payload_json FROM committees "
                "WHERE session = ? AND committee_id = ?",
                (session, committee_id),
            ).fetchone()
        return CommitteeRecord.model_validate_json(row[0]) if row else None

    def committees(self, session: int) -> list[CommitteeRecord]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT payload_json FROM committees WHERE session = ? "
                "ORDER BY committee_id",
                (session,),
            ).fetchall()
        return [CommitteeRecord.model_validate_json(r[0]) for r in rows]

    def bills_for(self, session: int, committee_id: str) -> list[BillRecord]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT b.payload_json FROM committee_bills cb "
                "JOIN bills b ON b.session = cb.session AND b.bill_id = cb.bill_id "
                "WHERE cb.session = ? AND cb.committee_id = ? ORDER BY cb.bill_id",
                (session, committee_id),
            ).fetchall()
        return [BillRecord.model_validate_json(r[0]) for r in rows]


class SqliteObservations:
    def __init__(self, db: _Db) -> None:
        self._db = db

    def append_many(self, observations: Sequence[Observation]) -> None:
        rows = [
            (
                str(o.observation_id),
                str(o.capture_id),
                o.observed_at.isoformat(),
                o.extractor_id,
                o.extractor_version,
                o.kind.value,
                o.session,
                o.bill_id,
                o.committee_id,
                o.model_dump_json(),
            )
            for o in observations
        ]
        with self._db.lock, self._db.conn:
            self._db.conn.executemany(
                "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
            )

    def for_bill(
        self, session: int, bill_id: str, kind: ObservationKind | None = None
    ) -> list[Observation]:
        sql = "SELECT payload_json FROM observations WHERE session = ? AND bill_id = ?"
        params: list[object] = [session, bill_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind.value)
        with self._db.lock:
            rows = self._db.conn.execute(
                sql + " ORDER BY observed_at", params
            ).fetchall()
        return [Observation.model_validate_json(r[0]) for r in rows]

    def get(self, observation_id: UUID) -> Observation:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT payload_json FROM observations WHERE observation_id = ?",
                (str(observation_id),),
            ).fetchone()
        if row is None:
            raise KeyError(str(observation_id))
        return Observation.model_validate_json(row[0])

    def of_kind(self, kind: ObservationKind) -> list[Observation]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT payload_json FROM observations WHERE kind = ? "
                "ORDER BY observed_at",
                (kind.value,),
            ).fetchall()
        return [Observation.model_validate_json(r[0]) for r in rows]

    def exists(
        self, capture_id: UUID, extractor_id: str, extractor_version: int
    ) -> bool:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT 1 FROM observations WHERE capture_id = ? "
                "AND extractor_id = ? AND extractor_version = ?",
                (str(capture_id), extractor_id, extractor_version),
            ).fetchone()
        return row is not None


class SqliteEvaluations:
    def __init__(self, db: _Db) -> None:
        self._db = db

    def append_many(self, evaluations: Sequence[TenureEvaluation]) -> None:
        evaluation_rows = [
            (
                str(e.evaluation_id),
                str(e.run_id),
                e.bill.session,
                e.bill.bill_id,
                e.committee_id,
                e.tenure_id,
                e.ruleset_id,
                e.ruleset_version,
                e.as_of.isoformat(),
                e.label.value,
                e.input_fingerprint,
                e.model_dump_json(),
            )
            for e in evaluations
        ]
        result_rows = [
            (
                str(e.evaluation_id),
                r.obligation_id,
                r.requirement_id,
                r.status.value,
                r.reason.value if r.reason else None,
                r.due.isoformat() if r.due else None,
                r.resolves_on.isoformat() if r.resolves_on else None,
                r.completed_on.isoformat() if r.completed_on else None,
            )
            for e in evaluations
            for r in e.results
        ]
        with self._db.lock, self._db.conn:
            self._db.conn.executemany(
                "INSERT INTO evaluations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                evaluation_rows,
            )
            self._db.conn.executemany(
                "INSERT INTO requirement_results VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                result_rows,
            )

    def for_run(self, run_id: UUID) -> list[TenureEvaluation]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT payload_json FROM evaluations WHERE run_id = ? "
                "ORDER BY bill_id, tenure_id",
                (str(run_id),),
            ).fetchall()
        return [TenureEvaluation.model_validate_json(r[0]) for r in rows]

    def latest_run(
        self, session: int, committee_id: str, ruleset_id: str
    ) -> UUID | None:
        with self._db.lock:
            row = self._db.conn.execute(
                "SELECT e.run_id FROM evaluations e JOIN runs r ON r.run_id = e.run_id "
                "WHERE e.session = ? AND e.committee_id = ? AND e.ruleset_id = ? "
                "AND r.status = ? ORDER BY r.started_at DESC LIMIT 1",
                (session, committee_id, ruleset_id, RunStatus.SUCCEEDED.value),
            ).fetchone()
        return UUID(row[0]) if row else None


class SqliteStore:
    """All repositories over one SQLite database."""

    def __init__(self, path: Path | str) -> None:
        db = _Db(path)
        self._db = db
        self.runs = SqliteRuns(db)
        self.captures = SqliteCaptures(db)
        self.inventory = SqliteInventory(db)
        self.observations = SqliteObservations(db)
        self.evaluations = SqliteEvaluations(db)

    def close(self) -> None:
        """Close the underlying connection."""
        self._db.conn.close()

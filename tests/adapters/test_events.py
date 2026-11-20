"""Tests for the event bus and sinks."""

import io
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from beacon.adapters.events import (
    ConsoleSink,
    EventBus,
    HeartbeatSink,
    JsonLinesFileSink,
    JsonLinesSink,
)
from beacon.adapters.sqlite_store import SqliteStore
from beacon.domain.events import (
    ItemFailed,
    Progress,
    RunEvent,
    RunFinished,
    RunStarted,
)
from beacon.ports.store import Stage

AT = datetime(2026, 11, 18, tzinfo=UTC)
RUN = uuid4()
EVENTS: list[RunEvent] = [
    RunStarted(run_id=RUN, stage="scrape", at=AT, scope="J19", total=2),
    Progress(
        run_id=RUN, stage="scrape", at=AT, done=1, total=2, item="H1", outcome="changed"
    ),
    ItemFailed(run_id=RUN, stage="scrape", at=AT, item="H2", error="HTTP 503"),
    Progress(
        run_id=RUN, stage="scrape", at=AT, done=2, total=2, item="H2", outcome="failed"
    ),
    RunFinished(
        run_id=RUN,
        stage="scrape",
        at=AT,
        status="succeeded",
        stats={"changed": 1, "failed": 1},
        duration_s=75.0,
    ),
]


class Recording:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def emit(self, event: RunEvent) -> None:
        self.events.append(event)


class Broken:
    def emit(self, event: RunEvent) -> None:
        raise RuntimeError("sink down")


def test_bus_isolates_failing_sinks():
    recording = Recording()
    bus = EventBus([Broken(), recording])
    for event in EVENTS:
        bus.emit(event)
    assert recording.events == EVENTS


def test_console_plain_output():
    stream = io.StringIO()
    sink = ConsoleSink(stream, plain_interval=0)
    for event in EVENTS:
        sink.emit(event)
    lines = stream.getvalue().splitlines()
    assert lines[0] == "scrape J19: started"
    assert "[" + "#" * 10 + "-" * 10 + "] 1/2 50% changed:1" in lines[1]
    assert lines[2] == "  failed H2: HTTP 503"
    assert "2/2 100% changed:1 failed:1" in lines[3]
    assert lines[4] == "scrape J19: succeeded in 1m15s changed=1 failed=1"
    assert stream.getvalue().isascii()


def test_json_lines_round_trip(tmp_path: Path):
    stream = io.StringIO()
    for event in EVENTS:
        JsonLinesSink(stream).emit(event)
        JsonLinesFileSink(tmp_path).emit(event)
    adapter = TypeAdapter(RunEvent)
    from_stream = [
        adapter.validate_json(line) for line in stream.getvalue().splitlines()
    ]
    from_file = [
        adapter.validate_json(line)
        for line in (tmp_path / f"{RUN}.jsonl").read_text().splitlines()
    ]
    assert from_stream == from_file == EVENTS


def test_heartbeat_records_progress(tmp_path: Path):
    store = SqliteStore(tmp_path / "beacon.db")
    run_id = store.runs.start(Stage.SCRAPE, "0", "cfg")
    sink = HeartbeatSink(store.runs, interval=0)
    for event in [e.model_copy(update={"run_id": run_id}) for e in EVENTS[:4]]:
        sink.emit(event)
    progress, beat = store.runs.progress(run_id)
    assert progress == {"changed": 1, "failed": 1, "done": 2, "total": 2}
    assert beat is not None
    store.close()


def test_heartbeat_throttles(tmp_path: Path):
    store = SqliteStore(tmp_path / "beacon.db")
    run_id = store.runs.start(Stage.SCRAPE, "0", "cfg")
    sink = HeartbeatSink(store.runs, interval=3600)
    sink.emit(EVENTS[0].model_copy(update={"run_id": run_id}))
    sink.emit(EVENTS[1].model_copy(update={"run_id": run_id}))
    progress, _ = store.runs.progress(run_id)
    assert progress == {}
    store.close()


def test_existing_database_gains_progress_columns(tmp_path: Path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, stage TEXT NOT NULL, "
        "started_at TEXT NOT NULL, finished_at TEXT, code_version TEXT NOT NULL, "
        "config_hash TEXT NOT NULL, status TEXT NOT NULL, stats_json TEXT)"
    )
    conn.commit()
    conn.close()
    store = SqliteStore(path)
    run_id = store.runs.start(Stage.COLLECT, "0", "cfg")
    store.runs.heartbeat(run_id, {"done": 1})
    assert store.runs.progress(run_id)[0] == {"done": 1}
    store.close()

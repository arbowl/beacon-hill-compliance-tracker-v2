"""Command-line entry point: one subcommand per stage, named plans, and serve."""

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import date
from importlib.metadata import version
from pathlib import Path
from random import SystemRandom

import uvicorn
from pydantic import ValidationError

from beacon.adapters.blobs import FileBlobStore
from beacon.adapters.clock import SystemClock
from beacon.adapters.events import ConsoleSink, JsonLinesSink
from beacon.adapters.run_lock import RunLockedError
from beacon.adapters.site_push import PushError, bundle_committees, site_client
from beacon.adapters.sqlite_store import SqliteStore
from beacon.config import Settings, load_settings
from beacon.domain.evaluation import ComplianceLabel
from beacon.evaluator.clauses import ClauseIndex
from beacon.gui.app import LOCAL_HOSTS, create_app
from beacon.orchestrator import runner, stages
from beacon.orchestrator.runner import PLANS, RunRequest
from beacon.ports.events import EventSink
from beacon.ports.store import Stage
from beacon.spotcheck import ledger
from beacon.spotcheck import run as spotcheck

_ANY_HOST = frozenset({"0.0.0.0", "::"})
DEFAULT_LEDGER = Path("audits/ledger.jsonl")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="beacon", description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("beacon.toml"))
    parser.add_argument("--committee", action="append", help="Committee id; repeatable")
    parser.add_argument("--as-of", help="Evaluation date, YYYY-MM-DD (noon ET)")
    parser.add_argument("--v1", type=Path, help="v1 basic_<committee>.json to compare")
    parser.add_argument("--quiet", action="store_true", help="No console progress")
    parser.add_argument(
        "--full", action="store_true", help="Run document finders for every bill"
    )
    parser.add_argument(
        "--json-events",
        action="store_true",
        help="Write events as JSON lines to stdout",
    )
    parser.add_argument(
        "--no-push", action="store_true", help="Do not push after `run mvp`"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for stage in Stage:
        commands.add_parser(stage.value)
    run = commands.add_parser("run", help="Run a named plan")
    run.add_argument("plan", choices=sorted(PLANS))
    commands.add_parser("push", help="Push the newest bundles to the site")
    serve = commands.add_parser("serve", help="Start the browser runner")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    spot = commands.add_parser("spotcheck", help="Sample evaluations for audit")
    spot.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    actions = spot.add_subparsers(dest="action", required=True)
    draw = actions.add_parser("sample", help="Write dossiers for a fresh sample")
    draw.add_argument("--count", type=int, default=5)
    draw.add_argument("--seed", type=int, help="Defaults to a random seed")
    draw.add_argument("--label", choices=[label.value for label in ComplianceLabel])
    verdicts = actions.add_parser("record", help="Append a sample's verdicts.json")
    verdicts.add_argument("directory", type=Path)
    actions.add_parser("summary", help="Summarize the ledger")
    return parser


def _spotcheck(settings: Settings, args: argparse.Namespace) -> int:
    clock = SystemClock()
    try:
        if args.action == "summary":
            print(ledger.summary(ledger.read(args.ledger)))
            return 0
        if args.action == "record":
            entries = spotcheck.record(args.directory, args.ledger, clock)
            print(
                f"  recorded {len(entries)} verdicts in {args.ledger}", file=sys.stderr
            )
            return 0
        store = SqliteStore(settings.db_path)
        sources = spotcheck.Sources(
            store=store,
            blobs=FileBlobStore(settings.blob_dir),
            clauses=ClauseIndex.load(settings.rules_dir),
            ruleset=stages.RULESETS[settings.ruleset],
            clock=clock,
            session=settings.session,
            code_version=version("beacon"),
        )
        request = spotcheck.SampleRequest(
            count=args.count,
            seed=args.seed
            if args.seed is not None
            else SystemRandom().randrange(10**6),
            committees=tuple(args.committee or ()),
            label=ComplianceLabel(args.label) if args.label else None,
        )
        try:
            directory = spotcheck.sample(
                sources, request, settings.data_dir / "spotcheck", args.ledger
            )
        finally:
            store.close()
    except (spotcheck.SpotcheckError, ledger.LedgerError, ValidationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(directory)
    return 0


def _serve(config: Path, host: str, port: int) -> int:
    allowed = ("*",) if host in _ANY_HOST else (*LOCAL_HOSTS, host)
    app = create_app(config, allowed_hosts=allowed)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def _push(settings: Settings, committees: Sequence[str] | None) -> int:
    key = os.environ.get(runner.SITE_KEY_ENV)
    if settings.site.url is None:
        print("error: set [site] url in the config to push", file=sys.stderr)
        return 1
    if not key:
        print(f"error: set {runner.SITE_KEY_ENV} to push", file=sys.stderr)
        return 1
    try:
        with site_client(settings.site.url, key) as client:
            outcome = runner.push(settings, client, committees)
    except PushError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    notes, state = outcome.changelog, outcome.run
    print(f"  changelog: {', '.join(notes.updated) or 'unchanged'}", file=sys.stderr)
    print(
        f"  push {state.push_id} {state.status}: {', '.join(state.committees)}",
        file=sys.stderr,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the requested stage, plan, or server."""
    args = _parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args.config, args.host, args.port)
    settings = load_settings(args.config)
    if args.command == "push":
        return _push(settings, args.committee)
    if args.command == "spotcheck":
        return _spotcheck(settings, args)
    request = RunRequest(
        plan=args.plan if args.command == "run" else None,
        stage=None if args.command == "run" else Stage(args.command),
        committees=tuple(args.committee or ()),
        as_of=date.fromisoformat(args.as_of) if args.as_of else None,
        full=args.full,
        v1=args.v1,
    )
    sinks: list[EventSink] = []
    if args.json_events:
        sinks.append(JsonLinesSink(sys.stdout))
    if not args.quiet:
        sinks.append(ConsoleSink(sys.stderr))
    written: list[Path] = []
    try:
        for path in runner.run(settings, request, sinks):
            print(f"  wrote {path}", file=sys.stderr)
            written.append(path)
    except RunLockedError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except stages.StageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    pushed = bundle_committees(written)
    if args.command == "run" and pushed and not args.no_push and settings.site.url:
        return _push(settings, pushed)
    return 0

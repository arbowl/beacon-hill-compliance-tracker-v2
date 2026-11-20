"""Command-line entry point: one subcommand per stage, named plans, and serve."""

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import uvicorn

from beacon.adapters.events import ConsoleSink, JsonLinesSink
from beacon.adapters.run_lock import RunLockedError
from beacon.adapters.site_push import PushError
from beacon.config import Settings, load_settings
from beacon.gui.app import create_app
from beacon.orchestrator import runner, stages
from beacon.orchestrator.runner import PLANS, RunRequest
from beacon.ports.events import EventSink
from beacon.ports.store import Stage
from beacon.reporters.site_bundle import BUNDLE_SUFFIX


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
    return parser


def _serve(config: Path, host: str, port: int) -> int:
    uvicorn.run(create_app(config), host=host, port=port, log_level="warning")
    return 0


def _push(settings: Settings, committees: Sequence[str] | None) -> int:
    key = os.environ.get(runner.SITE_KEY_ENV)
    if not key:
        print(f"error: set {runner.SITE_KEY_ENV} to push", file=sys.stderr)
        return 1
    try:
        notes = runner.push_changelog(settings, key)
        print(
            f"  changelog: {', '.join(notes.updated) or 'unchanged'}", file=sys.stderr
        )
        state = runner.push(settings, key, committees)
    except PushError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
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
    pushed = [
        p.name.removesuffix(BUNDLE_SUFFIX)
        for p in written
        if p.name.endswith(BUNDLE_SUFFIX)
    ]
    if args.command == "run" and pushed and not args.no_push and settings.site.url:
        return _push(settings, pushed)
    return 0

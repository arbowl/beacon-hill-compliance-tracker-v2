"""The browser runner: a Starlette app over the same runner, store, and events."""

import asyncio
from collections.abc import AsyncIterator
from importlib.resources import files
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from beacon.adapters.blobs import FileBlobStore
from beacon.adapters.sqlite_store import SqliteStore
from beacon.config import Settings, load_settings
from beacon.evaluator.clauses import ClauseIndex
from beacon.gui import views
from beacon.gui.broadcast import BroadcastSink
from beacon.gui.jobs import JobBusyError, JobManager, RunFunction
from beacon.orchestrator import runner
from beacon.orchestrator.runner import RunRequest

KEEPALIVE_SECONDS = 15.0


def create_app(config: Path) -> Starlette:
    """The app for a beacon.toml."""
    return build_app(load_settings(config))


def build_app(settings: Settings, run_function: RunFunction = runner.run) -> Starlette:
    """The app for explicit settings; run_function is replaceable for tests."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    broadcast = BroadcastSink()
    jobs = JobManager(settings, [broadcast], run_function)
    store = SqliteStore(settings.db_path)
    blobs = FileBlobStore(settings.blob_dir)
    clauses = ClauseIndex.load(settings.rules_dir)
    page = files("beacon.gui").joinpath("static/index.html").read_text(encoding="utf-8")

    async def index(_: Request) -> Response:
        return HTMLResponse(page)

    async def get_options(_: Request) -> Response:
        return JSONResponse(views.options(settings, store))

    async def start_run(request: Request) -> Response:
        try:
            run_request = RunRequest.model_validate(await request.json())
        except ValidationError as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        try:
            jobs.start(run_request)
        except JobBusyError as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        return JSONResponse(jobs.state.model_dump(mode="json"), status_code=202)

    async def get_job(_: Request) -> Response:
        return JSONResponse(jobs.state.model_dump(mode="json"))

    async def get_overview(_: Request) -> Response:
        return JSONResponse(views.overview(store, settings))

    async def get_runs(_: Request) -> Response:
        return JSONResponse([r.model_dump(mode="json") for r in store.runs.recent(50)])

    async def get_results(request: Request) -> Response:
        committee_id = request.path_params["committee_id"]
        return JSONResponse(views.committee_results(store, settings, committee_id))

    async def get_bill(request: Request) -> Response:
        detail = views.bill_detail(
            store,
            settings,
            clauses,
            request.query_params.get("committee", settings.committees[0]),
            request.path_params["bill_id"],
        )
        return JSONResponse(detail)

    async def get_observation(request: Request) -> Response:
        try:
            observation = store.observations.get(UUID(request.path_params["id"]))
        except (KeyError, ValueError):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(observation.model_dump(mode="json"))

    async def get_capture(request: Request) -> Response:
        try:
            capture = store.captures.get(UUID(request.path_params["id"]))
        except (KeyError, ValueError):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(capture.model_dump(mode="json"))

    async def get_capture_body(request: Request) -> Response:
        try:
            capture = store.captures.get(UUID(request.path_params["id"]))
        except (KeyError, ValueError):
            return Response("not found", status_code=404)
        if capture.content_hash is None:
            return Response("no body stored", status_code=404)
        return Response(
            blobs.get(capture.content_hash),
            media_type=capture.media_type or "application/octet-stream",
            headers={"Content-Security-Policy": "sandbox"},
        )

    async def stream_events(request: Request) -> Response:
        queue = broadcast.subscribe()

        async def events() -> AsyncIterator[str]:
            try:
                while not await request.is_disconnected():
                    try:
                        payload = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {payload}\n\n"
            finally:
                broadcast.unsubscribe(queue)

        return StreamingResponse(events(), media_type="text/event-stream")

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/options", get_options),
            Route("/api/overview", get_overview),
            Route("/api/runs", get_runs),
            Route("/api/runs", start_run, methods=["POST"]),
            Route("/api/job", get_job),
            Route("/api/events", stream_events),
            Route("/api/committees/{committee_id}/results", get_results),
            Route("/api/bills/{bill_id}", get_bill),
            Route("/api/observations/{id}", get_observation),
            Route("/api/captures/{id}", get_capture),
            Route("/api/captures/{id}/body", get_capture_body),
            Mount(
                "/reports",
                app=StaticFiles(directory=settings.report_dir, check_dir=False),
            ),
        ]
    )
    app.state.jobs = jobs
    app.state.broadcast = broadcast
    return app

"""The browser runner: a Starlette app over the same runner, store, and events."""

import asyncio
import os
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from beacon.adapters.blobs import FileBlobStore
from beacon.adapters.clock import SystemClock
from beacon.adapters.sqlite_store import SqliteStore
from beacon.config import Settings, load_settings
from beacon.evaluator.clauses import ClauseIndex
from beacon.gui import views
from beacon.gui.broadcast import BroadcastSink
from beacon.gui.control import (
    PREFERENCES_FILE,
    PreferencesStore,
    Schedule,
    TargetPusher,
    TargetUpdate,
)
from beacon.gui.jobs import (
    JobBusyError,
    JobManager,
    JobRequest,
    PushFunction,
    RunFunction,
)
from beacon.gui.scheduler import Scheduler
from beacon.orchestrator import runner
from beacon.orchestrator.runner import RunRequest
from beacon.ports.clock import Clock

KEEPALIVE_SECONDS = 15.0
LOCAL_HOSTS = ("127.0.0.1", "localhost")


def create_app(config: Path, allowed_hosts: Sequence[str] = LOCAL_HOSTS) -> Starlette:
    """The app for a beacon.toml, answering only requests addressed to allowed_hosts."""
    return build_app(load_settings(config), allowed_hosts=allowed_hosts)


def build_app(
    settings: Settings,
    run_function: RunFunction = runner.run,
    push_function: PushFunction | None = None,
    clock: Clock | None = None,
    allowed_hosts: Sequence[str] = LOCAL_HOSTS,
    environ: Mapping[str, str] = os.environ,
) -> Starlette:
    """The app for explicit settings; the run, push, and clock are replaceable."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    clock = clock or SystemClock()
    preferences = PreferencesStore(settings.data_dir / PREFERENCES_FILE)
    broadcast = BroadcastSink()
    jobs = JobManager(
        settings,
        [broadcast],
        run_function,
        push_function or TargetPusher(settings, preferences, environ),
        clock,
    )
    scheduler = Scheduler(preferences, jobs.start, clock)
    store = SqliteStore(settings.db_path)
    blobs = FileBlobStore(settings.blob_dir)
    clauses = ClauseIndex.load(settings.rules_dir)
    page = files("beacon.gui").joinpath("static/index.html").read_text(encoding="utf-8")

    async def index(_: Request) -> Response:
        return HTMLResponse(page)

    async def get_options(_: Request) -> Response:
        return JSONResponse(views.options(settings, store))

    def start_job(job: JobRequest) -> Response:
        try:
            jobs.start(job)
        except JobBusyError as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        return JSONResponse(jobs.state.model_dump(mode="json"), status_code=202)

    async def start_run(request: Request) -> Response:
        if not _same_origin(request):
            return _forbidden()
        body = await _parse(request, RunRequest)
        if not isinstance(body, RunRequest):
            return body
        auto = preferences.value.target.auto and body.as_of is None
        return start_job(JobRequest(run=body, push=auto))

    async def start_push(request: Request) -> Response:
        if not _same_origin(request):
            return _forbidden()
        return start_job(JobRequest(push=True))

    async def get_job(_: Request) -> Response:
        return JSONResponse(jobs.state.model_dump(mode="json"))

    def control_view() -> dict[str, Any]:
        return views.control(
            preferences.value, settings, environ, scheduler.next_due, scheduler.last
        )

    async def get_control(_: Request) -> Response:
        return JSONResponse(control_view())

    async def put_schedule(request: Request) -> Response:
        if not _same_origin(request):
            return _forbidden()
        body = await _parse(request, Schedule)
        if not isinstance(body, Schedule):
            return body
        preferences.replace(preferences.value.model_copy(update={"schedule": body}))
        scheduler.reschedule()
        return JSONResponse(control_view())

    async def put_target(request: Request) -> Response:
        if not _same_origin(request):
            return _forbidden()
        body = await _parse(request, TargetUpdate)
        if not isinstance(body, TargetUpdate):
            return body
        current = preferences.value
        target = body.apply(current.target)
        preferences.replace(current.model_copy(update={"target": target}))
        return JSONResponse(control_view())

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

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None]:
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()

    app = Starlette(
        lifespan=lifespan,
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)],
        routes=[
            Route("/", index),
            Route("/api/options", get_options),
            Route("/api/overview", get_overview),
            Route("/api/runs", get_runs),
            Route("/api/runs", start_run, methods=["POST"]),
            Route("/api/job", get_job),
            Route("/api/push", start_push, methods=["POST"]),
            Route("/api/control", get_control),
            Route("/api/schedule", put_schedule, methods=["PUT"]),
            Route("/api/target", put_target, methods=["PUT"]),
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
        ],
    )
    app.state.jobs = jobs
    app.state.broadcast = broadcast
    app.state.scheduler = scheduler
    return app


def _forbidden() -> Response:
    return JSONResponse({"error": "Cross-origin request refused"}, status_code=403)


def _same_origin(request: Request) -> bool:
    """False for a browser request sent by a page served from somewhere else."""
    origin = request.headers.get("origin")
    return origin is None or origin == f"{request.url.scheme}://{request.url.netloc}"


async def _parse[T: BaseModel](request: Request, model: type[T]) -> T | Response:
    try:
        return model.model_validate_json(await request.body())
    except ValidationError as error:
        messages = (e["msg"].removeprefix("Value error, ") for e in error.errors())
        return JSONResponse({"error": "; ".join(messages)}, status_code=422)

"""The HTTP transport: Starlette routes + WebSocket over the transport-neutral
core.

This is the ONLY server module allowed to import starlette (an optional
extra). Handlers validate with the schema models, delegate to the supervisor /
registry, and translate outcomes to status codes. Auth is an explicit check at
the top of every handler (not middleware): BaseHTTPMiddleware buffers
streaming bodies, and an explicit call is easier to follow and test."""

import asyncio
import base64
import contextlib
import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from pydantic import ValidationError
from pydantic_ai.usage import RunUsage
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..config import MultiModelSource, detect_active_providers
from ..images import image_cache_root, media_type_for_path
from ..runtime.permissions import Mode
from ..session import SessionManager, TranscriptStore
from . import jobs_view
from .auth import token_matches
from .host import HostClosed, TurnQueueFull
from .schema import AskAnswerIn, MessageIn, SessionIn, SetModelIn, SteerIn, WorkspaceIn
from .supervisor import SessionBusy, SessionSupervisor
from .workspaces import WorkspaceRegistry

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _cached_json(content, cache_control: str, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content,
        status_code=status_code,
        headers={"cache-control": cache_control},
    )


def _unauthorized(request: Request) -> JSONResponse | None:
    """None when the request carries a valid bearer token; the 401 otherwise."""
    token = request.app.state.token
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and token_matches(token, header[len("Bearer "):]):
        return None
    return _error(401, "unauthorized", "missing or invalid bearer token")


def _registry(request: Request | WebSocket) -> WorkspaceRegistry:
    return request.app.state.registry


def _supervisor(request: Request | WebSocket) -> SessionSupervisor:
    return request.app.state.supervisor


def _workspace(request: Request):
    return _registry(request).get(request.path_params["ws"])


def _session_exists(record, session_id: str) -> bool:
    manager = SessionManager(Path(record.path))
    return manager.session_path(session_id).exists()


async def _json_body(request: Request, model):
    try:
        return model(**await request.json())
    except (ValidationError, ValueError, TypeError) as exc:
        raise _BadBody(str(exc)) from exc


class _BadBody(Exception):
    pass


async def health(request: Request) -> Response:
    return _cached_json({"status": "ok"}, "no-cache")


async def list_workspaces(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    records = [r.as_dict() for r in _registry(request).list()]
    return _cached_json({"workspaces": records}, "max-age=60")


async def create_workspace(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    try:
        body = await _json_body(request, WorkspaceIn)
    except _BadBody as exc:
        return _error(400, "bad_request", str(exc))
    try:
        if body.path is not None:
            record = _registry(request).register(body.name, Path(body.path))
        else:
            record = _registry(request).create_managed(body.name, git_url=body.git_url)
    except ValueError as exc:
        return _error(400, "bad_request", str(exc))
    return JSONResponse(record.as_dict(), status_code=201)


async def delete_workspace(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    purge = request.query_params.get("purge") == "true"
    ws_id = request.path_params["ws"]
    supervisor = _supervisor(request)
    # Same refusal (and the same benign check-then-act window) as per-session
    # DELETE: a turn that starts between this check and the teardown below is
    # aborted by close_workspace, exactly as delete_session's close would.
    if supervisor.busy_sessions(ws_id):
        return _error(
            409, "busy",
            "workspace has sessions with running turns; interrupt them first",
        )
    try:
        _registry(request).delete(ws_id, purge=purge)
    except KeyError:
        return _error(404, "not_found", "unknown workspace")
    except ValueError as exc:
        return _error(400, "bad_request", str(exc))
    # The registry record is gone; reclaim every live host, bus, and cached
    # mode for the workspace so nothing keeps streaming from (or holding open)
    # a deleted — possibly purged — directory.
    await supervisor.close_workspace(ws_id)
    return JSONResponse({"deleted": True})


async def list_sessions(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    # Each row carries the same status/pending_asks a per-session GET would
    # return, so list consumers (e.g. the mobile app) don't need one detail
    # request per session. peek() is an in-memory lookup — no host is spawned.
    supervisor = _supervisor(request)
    infos = SessionManager(Path(record.path)).list()
    sessions = []
    for info in infos:
        host = supervisor.peek(record.id, info.id)
        row = asdict(info)
        row["model"] = _effective_model(host, info.model)
        sessions.append({
            **row,
            "status": host.status if host else "idle",
            "pending_asks": host.pending_asks() if host else [],
        })
    return _cached_json({"sessions": sessions}, "max-age=60")


_MODELS_TTL_SECONDS = 60.0


async def list_models(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    # Short in-process TTL cache: list_models fetches live provider catalogs, so
    # cache the assembled list briefly to keep repeated picker opens snappy.
    cache = request.app.state.models_cache
    now = time.monotonic()
    if cache["at"] is None or now - cache["at"] > _MODELS_TTL_SECONDS:
        entries = await MultiModelSource.from_env().list_models()
        cache["data"] = [
            {"id": e.qualified, "name": e.name, "provider": e.provider} for e in entries
        ]
        cache["at"] = now
    return _cached_json({"models": cache["data"]}, "max-age=300")


async def create_session(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    try:
        body = await _json_body(request, SessionIn)
    except _BadBody as exc:
        return _error(400, "bad_request", str(exc))
    if body.mode is not None and body.mode not in (m.value for m in Mode):
        return _error(400, "bad_request", f"unknown mode: {body.mode}")
    store = SessionManager(Path(record.path)).create(body.name)
    if body.model is not None:
        store.model = body.model
    # Persist the chosen mode on the session header (set before the initial
    # save below so it lands in the same write): the supervisor's in-memory
    # copy dies with the daemon, and host_for re-reads this field after a
    # restart or idle eviction.
    store.mode = body.mode
    # An immediate empty save makes the session file exist, so list/history/
    # message endpoints see it before its first turn.
    store.save([], RunUsage())
    if body.mode is not None:
        _supervisor(request).set_mode(record.id, store.session_id, Mode(body.mode))
    return JSONResponse({"id": store.session_id, "name": store.name}, status_code=201)


def _effective_model(host, info_model: str | None) -> str:
    """The model a session is actually running, never None. A loaded host is
    authoritative (reflects a live set_model and the resolved config default
    even when the header was never written); else the persisted header; else
    the configured default, resolved the same way bootstrap.py does — no
    Harness build required."""
    if host is not None and host.harness.model_id:
        return host.harness.model_id
    if info_model:
        return info_model
    configs, default_provider = detect_active_providers()
    return f"{default_provider}:{configs[default_provider].model or ''}"


async def get_session(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    infos = {i.id: i for i in SessionManager(Path(record.path)).list()}
    info = infos.get(session_id)
    if info is None:
        return _error(404, "not_found", "unknown session")
    host = _supervisor(request).peek(record.id, session_id)
    session_dict = asdict(info)
    session_dict["model"] = _effective_model(host, info.model)
    return _cached_json({
        "session": session_dict,
        "status": host.status if host else "idle",
        "queued": host.queued if host else 0,
        "pending_asks": host.pending_asks() if host else [],
    }, "no-cache")


async def delete_session(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    if not _session_exists(record, session_id):
        return _error(404, "not_found", "unknown session")
    host = _supervisor(request).peek(record.id, session_id)
    if host is not None and host.busy:
        return _error(409, "busy", "session has a running turn; interrupt it first")
    await _supervisor(request).close_host(record.id, session_id)
    SessionManager(Path(record.path)).delete(session_id)
    _supervisor(request).forget(record.id, session_id)
    return JSONResponse({"deleted": True})


async def post_message(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    if not _session_exists(record, session_id):
        return _error(404, "not_found", "unknown session")
    try:
        body = await _json_body(request, MessageIn)
    except _BadBody as exc:
        return _error(400, "bad_request", str(exc))
    attachments = None
    if body.attachments:
        try:
            attachments = [
                (base64.b64decode(a.data_b64), a.media_type) for a in body.attachments
            ]
        except ValueError:
            return _error(400, "bad_request", "invalid base64 in attachment data_b64")
    host = await _supervisor(request).host_for(record, session_id)
    try:
        turn_id = host.submit(body.prompt, attachments)
    except TurnQueueFull:
        return _error(429, "queue_full", "turn queue is full; wait for the running turn")
    except HostClosed:
        return _error(
            404, "host_closed",
            "session host was torn down, retry",
        )
    return JSONResponse({"turn_id": turn_id}, status_code=202)


async def post_interrupt(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    host = _supervisor(request).peek(record.id, request.path_params["sid"])
    interrupted = host.interrupt() if host is not None else False
    return JSONResponse({"interrupted": interrupted})


async def post_steer(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    try:
        body = await _json_body(request, SteerIn)
    except _BadBody as exc:
        return _error(400, "bad_request", str(exc))
    host = _supervisor(request).peek(record.id, request.path_params["sid"])
    if host is None or not host.busy:
        return _error(409, "not_running", "no running turn to steer")
    host.steer(body.text)
    return JSONResponse({"ok": True})


async def set_session_model(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    if not _session_exists(record, session_id):
        return _error(404, "not_found", "unknown session")
    try:
        body = await _json_body(request, SetModelIn)
    except _BadBody as exc:
        return _error(400, "bad_request", str(exc))
    try:
        _supervisor(request).set_model(record, session_id, body.model)
    except SessionBusy:
        return _error(409, "busy", "cannot switch models while a turn is running")
    return JSONResponse({"ok": True, "model": body.model})


def _spawn_meta_reader(record, session_id: str):
    """A ``stream_id -> meta | None`` closure over the session's persisted
    sidecar store. Rebuilt per request (cheap) so it always targets the session
    on disk, the same pattern SpawnTranscripts uses."""
    store = SessionManager(Path(record.path)).store(session_id)
    transcripts = TranscriptStore(store.path, store.session_id)

    def read(stream_id: str):
        try:
            return transcripts.read_meta(stream_id)
        except (OSError, ValueError):
            return None

    return read


async def list_jobs(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    if not _session_exists(record, session_id):
        return _error(404, "not_found", "unknown session")
    host = _supervisor(request).peek(record.id, session_id)
    if host is None:
        return _cached_json({"jobs": []}, "max-age=30")
    registry = host.harness.deps.jobs
    dtos = jobs_view.assemble(
        registry.list(), registry.history, _spawn_meta_reader(record, session_id)
    )
    return _cached_json({"jobs": dtos}, "max-age=30")


async def get_job(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    if not _session_exists(record, session_id):
        return _error(404, "not_found", "unknown session")
    job_id = request.path_params["job_id"]
    host = _supervisor(request).peek(record.id, session_id)
    if host is None:
        return _error(404, "job_not_found", "unknown job")
    registry = host.harness.deps.jobs
    job = registry.get(job_id)
    if job is not None:
        result = registry.output(job_id)
    else:
        job = next((j for j in registry.history if j.id == job_id), None)
        if job is None:
            return _error(404, "job_not_found", "unknown job")
        result = job.result or ""
    meta = None
    if job.kind == "agent" and job.stream_id:
        meta = _spawn_meta_reader(record, session_id)(job.stream_id)
    return _cached_json(jobs_view.detail_dto(job, result, meta), "max-age=30")


async def list_asks(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    host = _supervisor(request).peek(record.id, request.path_params["sid"])
    return _cached_json(
        {"asks": host.pending_asks() if host else []},
        "no-cache",
    )


async def answer_ask(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    try:
        body = await _json_body(request, AskAnswerIn)
    except _BadBody as exc:
        return _error(400, "bad_request", str(exc))
    host = _supervisor(request).peek(record.id, request.path_params["sid"])
    if host is None:
        return _error(404, "not_found", "no live session host")
    if not host.answer_ask(request.path_params["aid"], body.as_answer()):
        return _error(404, "not_found", "unknown or already-answered ask")
    return JSONResponse({"ok": True})


async def session_ws(websocket: WebSocket) -> None:
    """Live event stream over WebSocket.

    Auth is the Authorization: Bearer header on the upgrade.
    Resume via ?after_seq=<seq>; the synthetic stream.gap that EventBus.attach
    prepends when the resume point fell off the ring is delivered like any
    other event. One JSON text frame per Event (Event.as_dict()).

    A background pump forwards bus events; the main coroutine blocks on
    receive() purely to observe client disconnect, then cancels the pump —
    otherwise a dead client with no pending events would leak the
    subscription until the next publish."""
    token = websocket.app.state.token
    header = websocket.headers.get("authorization", "")
    if not (header.startswith("Bearer ")
            and token_matches(token, header[len("Bearer "):])):
        await websocket.close(code=4401)
        return
    record = _registry(websocket).get(websocket.path_params["ws"])
    session_id = websocket.path_params["sid"]
    if record is None or not _session_exists(record, session_id):
        await websocket.close(code=4404)
        return
    raw = websocket.query_params.get("after_seq", "")
    after_seq = int(raw) if raw.isdigit() else None
    bus = _supervisor(websocket).bus_for(record.id, session_id)

    await websocket.accept()
    subscription = bus.attach(after_seq=after_seq)

    async def pump() -> None:
        while True:
            # timeout=None (the default) blocks until an event is queued, so
            # next_event() only returns None on a timeout — this assert is
            # for the type checker, not a runtime possibility here.
            event = await subscription.next_event()
            assert event is not None
            await websocket.send_json(event.as_dict())

    pump_task = asyncio.ensure_future(pump())
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
        finally:
            subscription.close()


async def get_history(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    path = SessionManager(Path(record.path)).session_path(session_id)
    if not path.exists():
        return _error(404, "not_found", "unknown session")
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return _error(500, "unreadable", "session file is unreadable")
    messages = data.get("messages", [])
    try:
        offset = max(0, int(request.query_params.get("offset", "0")))
        limit = max(1, int(request.query_params.get("limit", "100")))
    except ValueError:
        return _error(400, "bad_request", "offset/limit must be integers")
    # The seq watermark this on-disk snapshot is consistent up to. Peek (never
    # create) the live bus: a session with no live bus (daemon just started,
    # host never mounted) has published nothing, so 0 is correct. The client
    # uses this to tell a resync echo (seq <= history_seq, already in these
    # messages) from an in-flight tail (seq > history_seq, not yet persisted).
    bus = _supervisor(request).bus_peek(record.id, session_id)
    history_seq = bus.history_seq if bus is not None else 0
    return _cached_json({
        "id": data.get("id"),
        "name": data.get("name"),
        "model": data.get("model"),
        "message_count": len(messages),
        "offset": offset,
        "history_seq": history_seq,
        "messages": messages[offset:offset + limit],
    }, "max-age=10")


async def get_session_image(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    if not _session_exists(record, session_id):
        return _error(404, "not_found", "unknown session")
    sha = request.path_params["sha"]
    if not _SHA_RE.fullmatch(sha):
        return _error(404, "not_found", "unknown image")
    for path in sorted((image_cache_root() / session_id).glob(f"{sha}.*")):
        media = media_type_for_path(path) or "application/octet-stream"
        try:
            data = path.read_bytes()
        except OSError:
            continue
        return Response(
            data, media_type=media,
            headers={"cache-control": "public, max-age=31536000, immutable"}
        )
    return _error(404, "not_found", "unknown image")


def create_app(
    *, registry: WorkspaceRegistry, supervisor: SessionSupervisor, token: str
) -> Starlette:
    base = "/v1/workspaces/{ws}/sessions/{sid}"
    routes = [
        Route("/v1/health", health, methods=["GET"]),
        Route("/v1/models", list_models, methods=["GET"]),
        Route("/v1/workspaces", list_workspaces, methods=["GET"]),
        Route("/v1/workspaces", create_workspace, methods=["POST"]),
        Route("/v1/workspaces/{ws}", delete_workspace, methods=["DELETE"]),
        Route("/v1/workspaces/{ws}/sessions", list_sessions, methods=["GET"]),
        Route("/v1/workspaces/{ws}/sessions", create_session, methods=["POST"]),
        Route(base, get_session, methods=["GET"]),
        Route(base, delete_session, methods=["DELETE"]),
        Route(f"{base}/messages", post_message, methods=["POST"]),
        Route(f"{base}/interrupt", post_interrupt, methods=["POST"]),
        Route(f"{base}/steer", post_steer, methods=["POST"]),
        Route(f"{base}/model", set_session_model, methods=["POST"]),
        Route(f"{base}/asks", list_asks, methods=["GET"]),
        Route(f"{base}/asks/{{aid}}", answer_ask, methods=["POST"]),
        Route(f"{base}/jobs", list_jobs, methods=["GET"]),
        Route(f"{base}/jobs/{{job_id}}", get_job, methods=["GET"]),
        WebSocketRoute(f"{base}/ws", session_ws),
        Route(f"{base}/history", get_history, methods=["GET"]),
        Route(f"{base}/images/{{sha}}", get_session_image, methods=["GET"]),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        supervisor.start_evictor()
        try:
            yield
        finally:
            # Graceful shutdown: interrupt running turns (resumable flush),
            # cancel parked asks, persist every host.
            await supervisor.aclose()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.registry = registry
    app.state.supervisor = supervisor
    app.state.token = token
    app.state.models_cache = {"at": None, "data": []}
    return app

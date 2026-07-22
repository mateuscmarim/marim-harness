"""The HTTP transport: Starlette routes + SSE over the transport-neutral core.

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
from dataclasses import asdict
from pathlib import Path

from pydantic import ValidationError
from pydantic_ai.usage import RunUsage
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..images import image_cache_root, media_type_for_path
from ..runtime.permissions import Mode
from ..session import SessionManager
from .auth import token_matches
from .host import HostClosed, TurnQueueFull
from .schema import AskAnswerIn, MessageIn, SessionIn, SteerIn, WorkspaceIn, sse_format
from .supervisor import SessionSupervisor
from .workspaces import WorkspaceRegistry

_HEARTBEAT_SECONDS = 15.0
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _unauthorized(request: Request) -> JSONResponse | None:
    """None when the request carries a valid token; the 401 response otherwise.
    The SSE endpoint additionally accepts ?access_token= because a browser
    EventSource cannot set headers."""
    token = request.app.state.token
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and token_matches(token, header[len("Bearer "):]):
        return None
    if request.url.path.endswith("/events"):
        presented = request.query_params.get("access_token")
        if presented and token_matches(token, presented):
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
    return JSONResponse({"status": "ok"})


async def list_workspaces(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    records = [r.as_dict() for r in _registry(request).list()]
    return JSONResponse({"workspaces": records})


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
    try:
        _registry(request).delete(request.path_params["ws"], purge=purge)
    except KeyError:
        return _error(404, "not_found", "unknown workspace")
    except ValueError as exc:
        return _error(400, "bad_request", str(exc))
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
        sessions.append({
            **asdict(info),
            "status": host.status if host else "idle",
            "pending_asks": host.pending_asks() if host else [],
        })
    return JSONResponse({"sessions": sessions})


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
    # An immediate empty save makes the session file exist, so list/history/
    # message endpoints see it before its first turn.
    store.save([], RunUsage())
    if body.mode is not None:
        _supervisor(request).set_mode(record.id, store.session_id, Mode(body.mode))
    return JSONResponse({"id": store.session_id, "name": store.name}, status_code=201)


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
    return JSONResponse({
        "session": asdict(info),
        "status": host.status if host else "idle",
        "queued": host.queued if host else 0,
        "pending_asks": host.pending_asks() if host else [],
    })


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


async def list_asks(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    host = _supervisor(request).peek(record.id, request.path_params["sid"])
    return JSONResponse({"asks": host.pending_asks() if host else []})


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


async def get_events(request: Request) -> Response:
    denied = _unauthorized(request)
    if denied:
        return denied
    record = _workspace(request)
    if record is None:
        return _error(404, "not_found", "unknown workspace")
    session_id = request.path_params["sid"]
    if not _session_exists(record, session_id):
        return _error(404, "not_found", "unknown session")
    bus = _supervisor(request).bus_for(record.id, session_id)
    last = request.headers.get("last-event-id", "")
    after_seq = int(last) if last.isdigit() else None

    async def stream():
        subscription = bus.attach(after_seq=after_seq)
        try:
            while True:
                event = await subscription.next_event(timeout=_HEARTBEAT_SECONDS)
                if event is None:
                    yield ": keepalive\n\n"  # comment frame keeps proxies open
                    continue
                yield sse_format(event)
        finally:
            subscription.close()

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


async def session_ws(websocket: WebSocket) -> None:
    """Live event stream over WebSocket — replaces GET .../events.

    Auth is the Authorization: Bearer header on the upgrade (okhttp can set
    it, unlike a browser EventSource, so there is no access_token fallback).
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
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task
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
    return JSONResponse({
        "id": data.get("id"),
        "name": data.get("name"),
        "model": data.get("model"),
        "message_count": len(messages),
        "offset": offset,
        "messages": messages[offset:offset + limit],
    })


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
        Route(f"{base}/asks", list_asks, methods=["GET"]),
        Route(f"{base}/asks/{{aid}}", answer_ask, methods=["POST"]),
        Route(f"{base}/events", get_events, methods=["GET"]),
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
    return app

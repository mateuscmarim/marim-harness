"""End-to-end HTTP surface: workspaces, sessions, turns, parked asks, SSE.

Runs the ASGI app under a *real* uvicorn server in a background thread and
drives it with a real httpx.Client over an actual TCP socket, rather than
Starlette's synchronous TestClient. This is a deliberate deviation from the
task brief (which specified TestClient): TestClient's transport (and real
httpx's ASGITransport underneath it) only returns a response once the whole
ASGI app coroutine has finished — `_TestClientTransport.handle_request`
blocks on `portal.call(self.app, scope, receive, send)` and builds the
`httpx.Response` from a fully-materialized buffer afterwards. That works fine
for ordinary request/response endpoints (confirmed: the workspace/session/
turn/ask tests below pass either way), but an SSE endpoint whose generator
runs `while True` never finishes on its own, and the fake transport's
`receive()` can only report `http.disconnect` *after* the response is
already complete — a chicken-and-egg deadlock with no way out (verified
empirically: both `starlette.testclient.TestClient` and a bare
`httpx.AsyncClient(transport=httpx.ASGITransport(app))` hang forever on a
trivial infinite SSE generator in this environment's installed versions
(starlette 1.3.1, httpx 0.28.1) — this is httpx's ASGI-transport-level
behavior, not a starlette regression). A real uvicorn server on a real
socket has no such limitation: the OS flushes bytes to the socket as they're
written, so a real client streams them incrementally. `uvicorn>=0.30` (the
same `[serve]`/dev-group dependency this task adds for `marim serve` itself)
already guards `capture_signals()` for non-main-thread use, so no Server
subclass is needed to run it in a background thread.

Note: this file defines its own `_make_deps`/`_make_harness` copies rather
than importing them from tests/conftest.py — bare `from conftest import ...`
does not resolve in this repo (tests/__init__.py makes the project root, not
tests/, the sys.path entry; verified with ModuleNotFoundError)."""

import asyncio
import json as _json
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from marim_harness.images import store_image
from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.server.http import create_app
from marim_harness.server.supervisor import SessionSupervisor
from marim_harness.server.workspaces import WorkspaceRegistry
from marim_harness.tools.provider import BuiltinToolProvider

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

_UI_HOOK_FIELDS = {
    "request_approval",
    "ask_user",
    "on_subagent_event",
    "on_subagent_notice",
    "on_subagent_model",
    "on_subagent_usage",
    "detach_fanout",
    "interactive",
    "notifier",
}


def _make_deps(root: Path, mode: Mode = Mode.auto, **kw) -> Deps:
    ui_kw = {k: kw.pop(k) for k in list(kw) if k in _UI_HOOK_FIELDS}
    return Deps(
        workspace=WorkspaceConfig(root=root, mode=mode),
        ui=UIHooks(**ui_kw) if ui_kw else UIHooks(),
        **kw,
    )


def _make_harness(model, deps, **config_kwargs) -> Harness:
    return Harness(
        model=model,
        provider=BuiltinToolProvider(),
        deps=deps,
        instructions="You are a coding agent.",
        **config_kwargs,
    )


def _edit_model() -> FunctionModel:
    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="read_file", args={"path": "a.txt"})]
            )
        if state["n"] == 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="edit_file",
                        args={
                            "path": "a.txt",
                            "edits": [{"old_string": "foo", "new_string": "bar"}],
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    stream_state = {"n": 0}

    async def stream_fn(messages, info):
        stream_state["n"] += 1
        if stream_state["n"] == 1:
            yield {
                0: DeltaToolCall(
                    name="read_file",
                    json_args=_json.dumps({"path": "a.txt"}),
                    tool_call_id="tc-read-1",
                )
            }
        elif stream_state["n"] == 2:
            yield {
                0: DeltaToolCall(
                    name="edit_file",
                    json_args=_json.dumps(
                        {"path": "a.txt", "edits": [{"old_string": "foo", "new_string": "bar"}]}
                    ),
                    tool_call_id="tc-edit-1",
                )
            }
        else:
            yield "done"

    return FunctionModel(fn, stream_function=stream_fn)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

    async def factory(
        workspace: Path,
        session_id: str,
        mode,
        project_memory_root: Path | None = None,
    ):
        from marim_harness.session import SessionManager

        manager = SessionManager(workspace)
        store = manager.store(session_id)
        deps = _make_deps(workspace, mode=mode or Mode.ask)
        return _make_harness(_edit_model(), deps, store=store, manager=manager)

    registry = WorkspaceRegistry(tmp_path / "state" / "workspaces.json", tmp_path / "managed")
    supervisor = SessionSupervisor(factory, idle_ttl=3600.0)
    app = create_app(registry=registry, supervisor=supervisor, token=TOKEN)

    # Real uvicorn server on an ephemeral loopback port, in a background
    # thread — see the module docstring for why this replaces TestClient.
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as test_client:
        yield test_client, tmp_path
    server.should_exit = True
    thread.join(timeout=10.0)


@pytest.fixture()
def client_with_supervisor(tmp_path, monkeypatch):
    """Sibling of ``client`` that ALSO yields the live ``SessionSupervisor``
    and the background event loop the uvicorn thread runs on.

    A new fixture rather than extending ``client``'s yield tuple: ``client``
    is consumed by every other test in this file via ``test_client, tmp_path
    = client`` unpacking, so widening its tuple would ripple through every
    caller. This duplicates the small amount of server-bootstrap plumbing
    instead, keeping those tests untouched.

    The loop matters because ``JobRegistry.register``/``wait`` are only safe
    to call while *that* event loop is current (``register`` schedules the
    coroutine via ``asyncio.ensure_future``, which binds to whatever loop is
    running); tests that want to seed jobs directly on a live host's registry
    must marshal onto it with ``asyncio.run_coroutine_threadsafe``."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

    async def factory(
        workspace: Path,
        session_id: str,
        mode,
        project_memory_root: Path | None = None,
    ):
        from marim_harness.session import SessionManager

        loop_holder["loop"] = asyncio.get_running_loop()
        manager = SessionManager(workspace)
        store = manager.store(session_id)
        deps = _make_deps(workspace, mode=mode or Mode.ask)
        return _make_harness(_edit_model(), deps, store=store, manager=manager)

    registry = WorkspaceRegistry(tmp_path / "state" / "workspaces.json", tmp_path / "managed")
    supervisor = SessionSupervisor(factory, idle_ttl=3600.0)
    app = create_app(registry=registry, supervisor=supervisor, token=TOKEN)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as test_client:
        yield test_client, tmp_path, supervisor, loop_holder
    server.should_exit = True
    thread.join(timeout=10.0)


def _setup_workspace_and_session(client, tmp_path, mode="ask"):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "a.txt").write_text("foo\n")
    ws = client.post(
        "/v1/workspaces", headers=AUTH, json={"name": "proj", "path": str(project)}
    ).json()
    sid = client.post(
        f"/v1/workspaces/{ws['id']}/sessions", headers=AUTH, json={"name": "run1", "mode": mode}
    ).json()["id"]
    return ws["id"], sid, project


def _poll(client, url, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(url, headers=AUTH)
        if response.status_code == 200 and predicate(response.json()):
            return response.json()
        time.sleep(0.05)
    raise AssertionError(f"timed out polling {url}")


def test_health_needs_no_auth(client):
    test_client, _ = client
    assert test_client.get("/v1/health").status_code == 200


def test_everything_else_requires_token(client):
    test_client, _ = client
    assert test_client.get("/v1/workspaces").status_code == 401
    assert (
        test_client.get("/v1/workspaces", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )


def test_workspace_crud(client):
    test_client, tmp_path = client
    project = tmp_path / "proj"
    project.mkdir()
    created = test_client.post(
        "/v1/workspaces", headers=AUTH, json={"name": "proj", "path": str(project)}
    )
    assert created.status_code == 201
    ws_id = created.json()["id"]
    listed = test_client.get("/v1/workspaces", headers=AUTH).json()
    assert [w["id"] for w in listed["workspaces"]] == [ws_id]
    managed = test_client.post("/v1/workspaces", headers=AUTH, json={"name": "scratch"})
    assert managed.json()["kind"] == "managed"
    assert test_client.delete(f"/v1/workspaces/{ws_id}", headers=AUTH).status_code == 200
    assert test_client.delete("/v1/workspaces/nope", headers=AUTH).status_code == 404


def test_session_create_and_list(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    sessions = test_client.get(f"/v1/workspaces/{ws_id}/sessions", headers=AUTH).json()
    assert sid in [s["id"] for s in sessions["sessions"]]
    detail = test_client.get(f"/v1/workspaces/{ws_id}/sessions/{sid}", headers=AUTH).json()
    assert detail["status"] == "idle"
    assert detail["pending_asks"] == []
    # A freshly created session has never mounted a host, so no live bus exists;
    # /history still serves the on-disk snapshot and reports history_seq 0.
    history = test_client.get(f"/v1/workspaces/{ws_id}/sessions/{sid}/history", headers=AUTH)
    assert history.json()["history_seq"] == 0
    missing = test_client.get(f"/v1/workspaces/{ws_id}/sessions/nope", headers=AUTH)
    assert missing.status_code == 404


def test_session_list_includes_status_and_pending_asks(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    listed = test_client.get(f"/v1/workspaces/{ws_id}/sessions", headers=AUTH).json()
    [row] = [s for s in listed["sessions"] if s["id"] == sid]
    assert row["status"] == "idle"
    assert row["pending_asks"] == []

    # With a live host parked on an approval, the list row must reflect it —
    # a client rendering the session list needs this without a per-session
    # detail request.
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    accepted = test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    assert accepted.status_code == 202
    listed = _poll(
        test_client,
        f"/v1/workspaces/{ws_id}/sessions",
        lambda body: any(s["id"] == sid and s["status"] == "waiting_ask" for s in body["sessions"]),
    )
    [row] = [s for s in listed["sessions"] if s["id"] == sid]
    [ask] = row["pending_asks"]
    assert ask["payload"]["tool_name"] == "edit_file"


def test_full_turn_with_parked_approval(client):
    test_client, tmp_path = client
    ws_id, sid, project = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"

    accepted = test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    assert accepted.status_code == 202

    # The edit parks for approval; answer it over the API.
    state = _poll(test_client, base, lambda s: s["status"] == "waiting_ask")
    [ask] = state["pending_asks"]
    assert ask["payload"]["tool_name"] == "edit_file"
    answered = test_client.post(f"{base}/asks/{ask['id']}", headers=AUTH, json={"approve": True})
    assert answered.status_code == 200

    _poll(test_client, base, lambda s: s["status"] == "idle")
    assert (project / "a.txt").read_text() == "bar\n"

    # History endpoint serves the persisted messages.
    history = test_client.get(f"{base}/history", headers=AUTH).json()
    assert history["message_count"] > 0
    assert len(history["messages"]) > 0
    # ...and reports the seq watermark its snapshot is consistent up to. After a
    # completed turn (turn.finished published post-persist) it is positive; the
    # client uses it to tell echoes from an in-flight tail across a resync.
    assert history["history_seq"] > 0


def test_interrupt_parked_turn(client):
    test_client, tmp_path = client
    ws_id, sid, project = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    _poll(test_client, base, lambda s: s["status"] == "waiting_ask")
    assert test_client.post(f"{base}/interrupt", headers=AUTH).json()["interrupted"] is True
    _poll(test_client, base, lambda s: s["status"] == "idle")
    assert (project / "a.txt").read_text() == "foo\n"
    # Nothing running now.
    assert test_client.post(f"{base}/interrupt", headers=AUTH).json()["interrupted"] is False


def test_steer_requires_running_turn(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    refused = test_client.post(f"{base}/steer", headers=AUTH, json={"text": "hey"})
    assert refused.status_code == 409
    test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    _poll(test_client, base, lambda s: s["status"] == "waiting_ask")
    assert test_client.post(f"{base}/steer", headers=AUTH, json={"text": "hey"}).status_code == 200
    test_client.post(f"{base}/interrupt", headers=AUTH)


def test_set_model_on_idle_session_persists(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    resp = test_client.post(f"{base}/model", headers=AUTH, json={"model": "claude-cli:opus"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "model": "claude-cli:opus"}
    detail = test_client.get(base, headers=AUTH).json()
    assert detail["session"]["model"] == "claude-cli:opus"


def test_set_model_rejected_while_running(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    # Park a turn on an approval -> host is busy (status != idle).
    test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    _poll(test_client, base, lambda s: s["status"] == "waiting_ask")
    refused = test_client.post(f"{base}/model", headers=AUTH, json={"model": "claude-cli:opus"})
    assert refused.status_code == 409
    test_client.post(f"{base}/interrupt", headers=AUTH)


def test_set_mode_on_idle_session_persists(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    resp = test_client.post(f"{base}/mode", headers=AUTH, json={"mode": "plan"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "mode": "plan"}
    detail = test_client.get(base, headers=AUTH).json()
    assert detail["session"]["mode"] == "plan"


def test_set_mode_rejected_while_running(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    _poll(test_client, base, lambda s: s["status"] == "waiting_ask")
    refused = test_client.post(f"{base}/mode", headers=AUTH, json={"mode": "plan"})
    assert refused.status_code == 409
    test_client.post(f"{base}/interrupt", headers=AUTH)


def test_set_mode_rejects_unknown_value(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    resp = test_client.post(f"{base}/mode", headers=AUTH, json={"mode": "yolo"})
    assert resp.status_code == 400


def test_unknown_workspace_and_session_404(client):
    test_client, _ = client
    assert test_client.get("/v1/workspaces/nope/sessions", headers=AUTH).status_code == 404
    assert (
        test_client.post(
            "/v1/workspaces/nope/sessions/x/messages", headers=AUTH, json={"prompt": "hi"}
        ).status_code
        == 404
    )


def test_malformed_base64_attachment_returns_400(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"

    # POST a message with an attachment containing malformed base64
    response = test_client.post(
        f"{base}/messages",
        headers=AUTH,
        json={
            "prompt": "process this",
            "attachments": [{"data_b64": "not-valid-base64!!!", "media_type": "image/png"}],
        },
    )
    assert response.status_code == 400
    error_body = response.json()
    assert error_body["error"]["code"] == "bad_request"
    assert "base64" in error_body["error"]["message"]


def test_session_image_requires_auth(client):
    test_client, _ = client
    # Auth is checked before workspace/session lookup, so placeholder ids
    # are enough to exercise the 401 path.
    response = test_client.get("/v1/workspaces/nope/sessions/nope/images/" + "0" * 64)
    assert response.status_code == 401


def test_session_image_roundtrip(client, monkeypatch):
    test_client, tmp_path = client
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "image-cache"))
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    data = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"
    cached = store_image(sid, data, "image/png")

    response = test_client.get(
        f"/v1/workspaces/{ws_id}/sessions/{sid}/images/{cached.sha}", headers=AUTH
    )
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_session_image_unknown_sha(client, monkeypatch):
    test_client, tmp_path = client
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "image-cache"))
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)

    valid_shape_sha = "a" * 64
    response = test_client.get(
        f"/v1/workspaces/{ws_id}/sessions/{sid}/images/{valid_shape_sha}", headers=AUTH
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_session_image_malformed_sha(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}/images"

    for bad_sha in ("abc", "A" * 64):
        response = test_client.get(f"{base}/{bad_sha}", headers=AUTH)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    # A traversal attempt embedded in the path segment: the router itself
    # will not route a slash-containing segment to a single {sha} param, so
    # this must 404 one way or another (route-miss or our regex check).
    traversal = test_client.get(f"{base}/../../../../etc/passwd", headers=AUTH)
    assert traversal.status_code == 404


def test_session_image_unknown_session(client):
    test_client, tmp_path = client
    project = tmp_path / "proj-image-unknown-session"
    project.mkdir()
    ws_id = test_client.post(
        "/v1/workspaces",
        headers=AUTH,
        json={"name": "proj-image-unknown-session", "path": str(project)},
    ).json()["id"]
    valid_shape_sha = "b" * 64
    response = test_client.get(
        f"/v1/workspaces/{ws_id}/sessions/nope/images/{valid_shape_sha}", headers=AUTH
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_session_image_unknown_workspace(client):
    test_client, _ = client
    valid_shape_sha = "c" * 64
    response = test_client.get(
        f"/v1/workspaces/nope/sessions/nope/images/{valid_shape_sha}", headers=AUTH
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_session_image_traversal_encoded(client):
    """Verify that percent-encoded traversal attempts get 404.

    ASGI/uvicorn decodes percent-escapes before routing, so a %2f-encoded
    slash decodes to "/" and doesn't match the {sha} converter [^/]+, yielding
    a plain 404 at the router. A slash-free encoding like %2e%2e decodes to
    "..", DOES reach the handler, and must 404 with the standard envelope."""
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}/images"

    # Construct the URLs manually to avoid httpx re-encoding or normalizing
    # dot-segments. httpx.URL accepts a path parameter that is passed
    # directly to the request without further normalization.

    # Case 1: ..%2f..%2fetc%2fpasswd decodes to ../../etc/passwd
    # The "/" after decoding doesn't match the {sha} converter [^/]+, so
    # the router itself rejects it with a plain 404 (no JSON envelope).
    encoded_with_slashes = f"{base}/..%2f..%2fetc%2fpasswd"
    response = test_client.get(encoded_with_slashes, headers=AUTH)
    assert response.status_code == 404
    # Router-level 404 is plain text, not JSON; we only check status here.

    # Case 2: %2e%2e decodes to "..", which has no slashes and DOES match
    # [^/]+, so it reaches the handler. The handler's sha regex check
    # rejects it, returning the standard error envelope.
    encoded_no_slashes = f"{base}/%2e%2e"
    response = test_client.get(encoded_no_slashes, headers=AUTH)
    assert response.status_code == 404
    # This case reaches the handler, so we get the JSON envelope.
    assert response.json()["error"]["code"] == "not_found"


def test_list_models_returns_qualified_entries(client, monkeypatch):
    test_client, _ = client
    from marim_harness.workspace import ModelEntry

    class _FakeSource:
        async def list_models(self):
            return [
                ModelEntry(
                    id="anthropic/claude-sonnet-4-6",
                    name="Claude Sonnet 4.6",
                    provider="openrouter",
                ),
                ModelEntry(id="sonnet", name="sonnet", provider="claude-cli"),
            ]

    monkeypatch.setattr(
        "marim_harness.server.http.MultiModelSource.from_env",
        classmethod(lambda cls: _FakeSource()),
    )
    unauth = test_client.get("/v1/models")
    assert unauth.status_code == 401
    body = test_client.get("/v1/models", headers=AUTH).json()
    ids = {m["id"] for m in body["models"]}
    assert "openrouter:anthropic/claude-sonnet-4-6" in ids
    assert "claude-cli:sonnet" in ids
    entry = next(m for m in body["models"] if m["id"] == "claude-cli:sonnet")
    assert entry["name"] == "sonnet"
    assert entry["provider"] == "claude-cli"


def test_create_session_with_model_persists(client):
    test_client, tmp_path = client
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    ws = test_client.post(
        "/v1/workspaces", headers=AUTH, json={"name": "proj", "path": str(project)}
    ).json()
    created = test_client.post(
        f"/v1/workspaces/{ws['id']}/sessions",
        headers=AUTH,
        json={"name": "run1", "model": "claude-cli:opus"},
    )
    assert created.status_code == 201
    sid = created.json()["id"]
    detail = test_client.get(f"/v1/workspaces/{ws['id']}/sessions/{sid}", headers=AUTH).json()
    assert detail["session"]["model"] == "claude-cli:opus"


def test_live_session_fields_carry_the_backend_context_and_quota():
    """``GET session`` answers the CLI backend's own context reading and
    quota hint next to the usage split; both are null under marim's own
    providers (no report), and every live field is None on a cold session."""
    from types import SimpleNamespace

    from pydantic_ai.usage import RunUsage

    from marim_harness.config.context_report import ContextReport
    from marim_harness.config.quota import QuotaHint, QuotaWindow
    from marim_harness.server.http import _live_session_fields

    assert _live_session_fields(None) == {
        "usage": None,
        "compact_threshold": None,
        "context": None,
        "quota": None,
    }
    session = SimpleNamespace(usage=RunUsage(), compact_threshold=100_000, history=[])
    cli = SimpleNamespace(
        context_report=ContextReport(27_516, 200_000),
        quota_hint=QuotaHint(QuotaWindow(11, 300), QuotaWindow(59, 10080)),
    )
    host = SimpleNamespace(
        harness=SimpleNamespace(session=session, model_id="x", current_model=cli)
    )
    fields = _live_session_fields(host)
    assert fields["context"] == {"used": 27_516, "window": 200_000}
    assert fields["quota"] == "quota 11% (5h) · 59% (1w)"
    assert fields["compact_threshold"] == 100_000
    host.harness.current_model = SimpleNamespace()  # one of marim's own providers
    fields = _live_session_fields(host)
    assert fields["context"] is None and fields["quota"] is None


def test_effective_model_prefers_loaded_host():
    from types import SimpleNamespace

    from marim_harness.server.http import _effective_model

    host = SimpleNamespace(harness=SimpleNamespace(model_id="openrouter:x/y"))
    assert _effective_model(host, "claude-cli:opus") == "openrouter:x/y"


def test_effective_model_falls_back_to_header():
    from marim_harness.server.http import _effective_model

    assert _effective_model(None, "claude-cli:opus") == "claude-cli:opus"


def test_effective_model_resolves_configured_default(monkeypatch):
    from marim_harness.config.model import ModelConfig
    from marim_harness.server.http import _effective_model

    cfg = ModelConfig(provider="claude-cli", model="sonnet")
    monkeypatch.setattr(
        "marim_harness.server.http.detect_active_providers",
        lambda: ({"claude-cli": cfg}, "claude-cli"),
    )
    assert _effective_model(None, None) == "claude-cli:sonnet"


def test_effective_model_falls_through_when_host_model_id_blank(monkeypatch):
    from types import SimpleNamespace

    from marim_harness.config.model import ModelConfig
    from marim_harness.server.http import _effective_model

    cfg = ModelConfig(provider="claude-cli", model="sonnet")
    monkeypatch.setattr(
        "marim_harness.server.http.detect_active_providers",
        lambda: ({"claude-cli": cfg}, "claude-cli"),
    )
    host = SimpleNamespace(harness=SimpleNamespace(model_id=None))
    assert _effective_model(host, None) == "claude-cli:sonnet"


def test_effective_model_falls_through_when_header_blank(monkeypatch):
    from marim_harness.config.model import ModelConfig
    from marim_harness.server.http import _effective_model

    cfg = ModelConfig(provider="claude-cli", model="sonnet")
    monkeypatch.setattr(
        "marim_harness.server.http.detect_active_providers",
        lambda: ({"claude-cli": cfg}, "claude-cli"),
    )
    assert _effective_model(None, "") == "claude-cli:sonnet"


def test_get_session_reports_default_when_header_null(client, monkeypatch):
    from marim_harness.config.model import ModelConfig

    cfg = ModelConfig(provider="claude-cli", model="sonnet")
    monkeypatch.setattr(
        "marim_harness.server.http.detect_active_providers",
        lambda: ({"claude-cli": cfg}, "claude-cli"),
    )
    test_client, tmp_path = client
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    ws = test_client.post(
        "/v1/workspaces", headers=AUTH, json={"name": "proj", "path": str(project)}
    ).json()
    created = test_client.post(
        f"/v1/workspaces/{ws['id']}/sessions", headers=AUTH, json={"name": "run1"}
    )
    sid = created.json()["id"]
    detail = test_client.get(f"/v1/workspaces/{ws['id']}/sessions/{sid}", headers=AUTH).json()
    assert detail["session"]["model"] == "claude-cli:sonnet"


def test_list_sessions_reports_effective_model(client, monkeypatch):
    from marim_harness.config.model import ModelConfig

    cfg = ModelConfig(provider="claude-cli", model="sonnet")
    monkeypatch.setattr(
        "marim_harness.server.http.detect_active_providers",
        lambda: ({"claude-cli": cfg}, "claude-cli"),
    )
    test_client, tmp_path = client
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    ws = test_client.post(
        "/v1/workspaces", headers=AUTH, json={"name": "proj", "path": str(project)}
    ).json()
    test_client.post(f"/v1/workspaces/{ws['id']}/sessions", headers=AUTH, json={"name": "run1"})
    rows = test_client.get(f"/v1/workspaces/{ws['id']}/sessions", headers=AUTH).json()["sessions"]
    assert rows[0]["model"] == "claude-cli:sonnet"


def test_session_mode_persisted_and_listed(client):
    """The mode chosen at session creation lands on the session file header
    (so it survives daemon restarts — see the supervisor tests for the
    read-back path) and surfaces in list responses."""
    test_client, tmp_path = client
    ws_id, sid, project = _setup_workspace_and_session(test_client, tmp_path, mode="plan")
    rows = test_client.get(f"/v1/workspaces/{ws_id}/sessions", headers=AUTH).json()["sessions"]
    assert {r["id"]: r["mode"] for r in rows}[sid] == "plan"

    from marim_harness.session.store import SessionManager

    assert SessionManager(project).store(sid).mode == "plan"


def test_delete_workspace_refuses_while_running_then_cleans_up(client):
    """Workspace DELETE mirrors per-session DELETE: 409 while any session in
    it has a running turn; on success the workspace (and its live state) is
    gone."""
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    _poll(test_client, base, lambda s: s["status"] == "waiting_ask")

    refused = test_client.delete(f"/v1/workspaces/{ws_id}", headers=AUTH)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "busy"

    test_client.post(f"{base}/interrupt", headers=AUTH)
    _poll(test_client, base, lambda s: s["status"] == "idle")
    assert test_client.delete(f"/v1/workspaces/{ws_id}", headers=AUTH).json()["deleted"] is True
    assert test_client.get(f"/v1/workspaces/{ws_id}/sessions", headers=AUTH).status_code == 404


def test_jobs_empty_for_idle_session(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    resp = test_client.get(f"/v1/workspaces/{ws_id}/sessions/{sid}/jobs", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"jobs": []}


def test_jobs_requires_auth_and_valid_session(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    assert test_client.get(f"/v1/workspaces/{ws_id}/sessions/{sid}/jobs").status_code == 401
    assert (
        test_client.get(f"/v1/workspaces/nope/sessions/{sid}/jobs", headers=AUTH).status_code == 404
    )
    assert (
        test_client.get(f"/v1/workspaces/{ws_id}/sessions/nope/jobs", headers=AUTH).status_code
        == 404
    )


def test_job_detail_404_for_unknown_id(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    # No live host on an idle session -> unknown job.
    resp = test_client.get(f"/v1/workspaces/{ws_id}/sessions/{sid}/jobs/job-99", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "job_not_found"


async def _register_and_settle_two_jobs(registry):
    """Register one bash job and one agent job directly on a live host's
    ``JobRegistry`` and block until both are settled. Must run ON the
    registry's own event loop (see ``client_with_supervisor``) because
    ``register()`` schedules its coroutine via ``asyncio.ensure_future``,
    which binds to whichever loop is current when it's called.

    The ``asyncio.sleep`` between the two registrations guarantees the agent
    job's ``finished_at`` sorts strictly after the bash job's, so the list
    endpoint's settled-by-finished_at-desc ordering is deterministic rather
    than a same-microsecond coin flip."""

    async def _const(value: str) -> str:
        return value

    bash_id = registry.register(
        "bash",
        "run the test suite",
        _const("full bash output line one\nfull bash output line two\n"),
        prompt="echo hi",
    )
    await registry.wait(bash_id)
    await asyncio.sleep(0.01)
    agent_id = registry.register(
        "agent",
        "explore: investigate the widget",
        _const("the agent's full synthesized result"),
        stream_id="tc-1",
        prompt="do the thing",
    )
    await registry.wait(agent_id)
    return bash_id, agent_id


def test_jobs_list_and_detail_for_live_bash_and_agent_jobs(client_with_supervisor):
    """Consolidated live-host coverage for BOTH job routes and BOTH job kinds:
    a real host (mounted by driving a turn to a parked approval, answering
    it, and polling back to idle) with a settled bash job and a settled agent
    job registered directly on its JobRegistry.

    Asserts: the list route returns both jobs, settled-desc ordered, with the
    agent row carrying real usage/tool_count/duration_secs read off a v2
    transcript sidecar and the bash row leaving those null; the detail route
    returns the exact prompt and the exact, full result for each job kind —
    the assertion that guards jobs_view.detail_dto's positional-arg assembly
    (job, result, meta), which a keyword-arg-only unit test can't catch."""
    test_client, tmp_path, supervisor, loop_holder = client_with_supervisor
    ws_id, sid, project = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"

    # Drive one real turn so a host mounts, then let it settle back to idle —
    # `peek` only returns non-None with a live host, and idle_ttl (3600s) is
    # large enough that it stays mounted for the rest of this test.
    test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    state = _poll(test_client, base, lambda s: s["status"] == "waiting_ask")
    [ask] = state["pending_asks"]
    test_client.post(f"{base}/asks/{ask['id']}", headers=AUTH, json={"approve": True})
    _poll(test_client, base, lambda s: s["status"] == "idle")

    host = supervisor.peek(ws_id, sid)
    assert host is not None, "expected a live host after a turn settled back to idle"
    registry = host.harness.deps.jobs

    loop = loop_holder["loop"]
    bash_id, agent_id = asyncio.run_coroutine_threadsafe(
        _register_and_settle_two_jobs(registry), loop
    ).result(timeout=5.0)

    # Write a real v2 transcript sidecar for the agent job's stream_id so
    # job_to_dto's meta enrichment reads real usage/tool_count/duration off
    # disk, exactly as a completed sub-agent spawn would leave behind.
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from marim_harness.session import SessionManager, TranscriptStore

    store = SessionManager(project).store(sid)
    transcripts = TranscriptStore(store.path, store.session_id)
    transcripts.write(
        "tc-1",
        [ModelRequest(parts=[UserPromptPart(content="investigate the widget")])],
        cap=2000,
        meta={
            "usage": {"input": 111, "output": 222},
            "tool_count": 4,
            "duration": 7.5,
            "type": "explore",
            "task": "investigate the widget",
            "status": "done",
        },
    )

    # --- GET .../jobs (list): both jobs, settled-desc ordered, meta only on
    # the agent row. ---
    listed = test_client.get(f"{base}/jobs", headers=AUTH)
    assert listed.status_code == 200
    rows = listed.json()["jobs"]
    ids = [r["id"] for r in rows]
    assert set(ids) == {bash_id, agent_id}
    # Both settled -> sorted by finished_at descending; the agent job settled
    # strictly later (see the sleep in the register helper), so it sorts first.
    assert ids == [agent_id, bash_id]

    by_id = {r["id"]: r for r in rows}
    agent_row = by_id[agent_id]
    assert agent_row["kind"] == "agent"
    assert agent_row["usage"] == {"input": 111, "output": 222}
    assert agent_row["tool_count"] == 4
    assert agent_row["duration_secs"] == 7.5

    bash_row = by_id[bash_id]
    assert bash_row["kind"] == "bash"
    assert bash_row["usage"] is None
    assert bash_row["tool_count"] is None
    assert bash_row["duration_secs"] is None

    # --- GET .../jobs/{job_id} (detail): exact prompt + exact full result,
    # for EACH kind. This guards detail_dto(job, result, meta) positional
    # assembly in http.py. ---
    bash_detail = test_client.get(f"{base}/jobs/{bash_id}", headers=AUTH)
    assert bash_detail.status_code == 200
    bash_body = bash_detail.json()
    assert bash_body["prompt"] == "echo hi"
    assert bash_body["result"] == "full bash output line one\nfull bash output line two\n"
    assert bash_body["kind"] == "bash"
    assert bash_body["usage"] is None

    agent_detail = test_client.get(f"{base}/jobs/{agent_id}", headers=AUTH)
    assert agent_detail.status_code == 200
    agent_body = agent_detail.json()
    assert agent_body["prompt"] == "do the thing"
    assert agent_body["result"] == "the agent's full synthesized result"
    assert agent_body["kind"] == "agent"
    assert agent_body["usage"] == {"input": 111, "output": 222}
    assert agent_body["tool_count"] == 4
    assert agent_body["duration_secs"] == 7.5


async def _observed_codex_handle(host):
    from marim_harness.codex.server import ThreadHandle
    from marim_harness.config.codex_cli_model import CodexCliModel
    from marim_harness.runtime.backend_jobs import CodexJobObserver

    async def decline(_method, _params):
        return {"decision": "denied"}

    model = CodexCliModel(None)
    host.harness.wire_cli_model(model)
    assert model.job_registry is host.harness.deps.jobs
    return ThreadHandle(
        thread_id="cli-parent",
        events=asyncio.Queue(),
        request_handler=decline,
        on_observation=CodexJobObserver(model.job_registry, "cli-parent", model.on_jobs_settled),
    )


async def _feed_codex_observations(handle, notifications):
    for method, params in notifications:
        handle.observe(method, params)
    # Let scheduled wake callbacks run before crossing back to HTTP.
    await asyncio.sleep(0)


async def _observed_job_events(host, after_seq):
    subscription = host.bus.attach(after_seq=after_seq)
    try:
        return [
            await subscription.next_event(timeout=1) for _ in range(host.bus.last_seq - after_seq)
        ]
    finally:
        subscription.close()


@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled"])
def test_codex_observations_reach_http_jobs_after_parent_turn_ends(client_with_supervisor, outcome):
    """Raw backend events update the real host, API and jobs.changed bus while idle.

    No transcript consumer drains the handle, and no provider call runs after
    the initial fixture turn. This catches registering cards only in the live
    rendering path, or treating parent turn completion as child completion.
    """
    from marim_harness.session import SessionManager

    test_client, tmp_path, supervisor, loop_holder = client_with_supervisor
    ws_id, sid, project = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    _mount_idle_host(test_client, base)
    host = supervisor.peek(ws_id, sid)
    assert host is not None
    loop = loop_holder["loop"]
    handle = asyncio.run_coroutine_threadsafe(_observed_codex_handle(host), loop).result(timeout=5)
    cursor = host.bus.last_seq
    spawn = {
        "type": "collabAgentToolCall",
        "id": "cli-spawn",
        "tool": "spawnAgent",
        "receiverThreadIds": ["cli-child"],
        "prompt": "Implement and verify file download support.",
        "status": "inProgress",
    }
    initial = [
        ("item/started", {"threadId": "cli-parent", "item": spawn}),
        ("item/started", {"threadId": "cli-parent", "item": spawn}),
        ("turn/completed", {"threadId": "cli-parent", "turn": {"status": "completed"}}),
    ]
    asyncio.run_coroutine_threadsafe(_feed_codex_observations(handle, initial), loop).result(
        timeout=5
    )
    listed = test_client.get(f"{base}/jobs", headers=AUTH)
    assert listed.status_code == 200
    [running] = listed.json()["jobs"]
    assert running["status"] == "running" and running["backend_owned"] is True
    assert running["stream_id"] == "cli-spawn" and running["started_at"]
    job_url = f"{base}/jobs/{running['id']}"
    refused = test_client.post(f"{job_url}/cancel", headers=AUTH)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "backend_managed"
    assert test_client.get(job_url, headers=AUTH).json()["status"] == "running"

    report = "Detailed implementation report\n" + "verified result\n" * 40
    terminal_item = {
        **spawn,
        "status": "completed",
        "agentsStates": {"cli-child": {"status": "completed" if outcome == "done" else "errored"}},
    }
    if outcome == "cancelled":
        terminal_item = {
            "type": "subAgentActivity",
            "id": "cli-spawn",
            "agentThreadId": "cli-child",
            "kind": "interrupted",
        }
    terminal_method = "item/started" if outcome == "cancelled" else "item/completed"
    terminal = (terminal_method, {"threadId": "cli-parent", "item": terminal_item})
    notifications = [
        ("item/agentMessage/delta", {"threadId": "cli-child", "itemId": "report", "delta": report}),
        terminal,
        terminal,
    ]
    asyncio.run_coroutine_threadsafe(_feed_codex_observations(handle, notifications), loop).result(
        timeout=5
    )
    response = test_client.get(job_url, headers=AUTH)
    assert response.status_code == 200
    detail = response.json()
    assert (detail["status"], detail["result"], detail["prompt"]) == (
        outcome,
        report,
        spawn["prompt"],
    )
    assert detail["finished_at"]
    assert test_client.get(base, headers=AUTH).json()["status"] == "idle"
    assert not host.harness.deps.jobs.has_finished_pending()
    assert host.harness.deps.jobs.take_finished_digest() == ""
    events = asyncio.run_coroutine_threadsafe(_observed_job_events(host, cursor), loop).result(
        timeout=5
    )
    assert [event.type for event in events] == ["jobs.changed", "jobs.changed"]
    # Idle completion is metadata-only: the unchanged transcript version must
    # not make session persistence skip the job result until the next turn.
    asyncio.run_coroutine_threadsafe(host.harness.cli_job_persistence.flush(), loop).result(
        timeout=5
    )
    [saved] = SessionManager(project).persisted_jobs(sid)
    assert (saved["id"], saved["status"], saved["backend_owned"]) == (running["id"], outcome, True)


def test_jobs_list_on_a_cold_session_returns_its_persisted_history(client):
    """Phase 4b: a session with no live host still answers ``GET jobs`` with
    the settled rows its file carries (``jobs``, the export the in-process
    panel imports as history), so an attached TUI's replay can settle a
    spawn card that finished in an earlier daemon life. Rows are settled by
    definition, so ``result_tail`` is the persisted tail."""
    from pydantic_ai.usage import RunUsage

    from marim_harness.session import SessionManager

    test_client, tmp_path = client
    ws_id, sid, project = _setup_workspace_and_session(test_client, tmp_path)
    store = SessionManager(project).store(sid)
    store.save(
        [],
        RunUsage(),
        jobs=[
            {
                "id": "job-1",
                "kind": "agent",
                "label": "explore: old",
                "status": "done",
                "result_tail": "it worked",
                "stream_id": "sg-old",
                "finished_at": "2026-09-13T00:00:00+00:00",
            }
        ],
    )
    resp = test_client.get(f"/v1/workspaces/{ws_id}/sessions/{sid}/jobs", headers=AUTH)
    assert resp.status_code == 200
    [row] = resp.json()["jobs"]
    assert (row["id"], row["status"], row["stream_id"]) == ("job-1", "done", "sg-old")
    assert row["result_tail"] == "it worked"
    # Cold: no live job to cancel (history rows are settled by definition).
    cancel = test_client.post(
        f"/v1/workspaces/{ws_id}/sessions/{sid}/jobs/job-1/cancel", headers=AUTH
    )
    assert cancel.status_code == 404
    assert cancel.json()["error"]["code"] == "job_not_found"


def test_cancel_job_stops_a_running_job_and_refuses_a_settled_one(client_with_supervisor):
    """Phase 4b: ``POST .../jobs/{id}/cancel`` cancels a live job with the
    registry's own wording riding back, a second cancel is a 409 (settled),
    an unknown id a 404, and the listing shows the job ``cancelled``."""
    test_client, tmp_path, supervisor, loop_holder = client_with_supervisor
    ws_id, sid, _project = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    _mount_idle_host(test_client, base)
    host = supervisor.peek(ws_id, sid)
    assert host is not None
    registry = host.harness.deps.jobs
    loop = loop_holder["loop"]

    async def _register_forever() -> str:
        async def _forever() -> str:
            await asyncio.Event().wait()
            return "never"

        return registry.register("bash", "sleep forever", _forever(), prompt="sleep")

    job_id = asyncio.run_coroutine_threadsafe(_register_forever(), loop).result(timeout=5.0)

    unknown = test_client.post(f"{base}/jobs/job-99/cancel", headers=AUTH)
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "job_not_found"
    assert test_client.post(f"{base}/jobs/{job_id}/cancel").status_code == 401

    cancelled = test_client.post(f"{base}/jobs/{job_id}/cancel", headers=AUTH)
    assert cancelled.status_code == 200
    assert cancelled.json() == {"ok": True, "message": f"cancelled {job_id}"}
    [row] = test_client.get(f"{base}/jobs", headers=AUTH).json()["jobs"]
    assert (row["id"], row["status"]) == (job_id, "cancelled")

    again = test_client.post(f"{base}/jobs/{job_id}/cancel", headers=AUTH)
    assert again.status_code == 409
    assert again.json()["error"] == {
        "code": "already_settled",
        "message": f"job {job_id} already cancelled",
    }


def test_resume_spawn_loads_the_host_and_maps_the_runners_answer(client_with_supervisor):
    """Phase 4b: ``POST .../subagents/{stream_id}/resume`` reaches the
    harness's resume seam even on a cold session (the host is loaded like
    ``POST messages`` does); the runner's refusal is a 409 carrying its
    reason, a started resume is a 201 with the job id, and a session whose
    harness has no resume seam refuses too."""
    test_client, tmp_path, supervisor, _loop_holder = client_with_supervisor
    ws_id, sid, _project = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    assert supervisor.peek(ws_id, sid) is None  # cold

    assert test_client.post(f"{base}/subagents/sg-1/resume").status_code == 401
    refused = test_client.post(f"{base}/subagents/sg-never/resume", headers=AUTH)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "resume_refused"
    assert "No resumable transcript" in refused.json()["error"]["message"]
    host = supervisor.peek(ws_id, sid)
    assert host is not None  # the resume loaded it

    seen: list[str] = []

    async def fake_resume(stream_id: str) -> tuple[str | None, str]:
        seen.append(stream_id)
        return "job-7", "resumed"

    host.harness.deps.services.resume_subagent = fake_resume
    started = test_client.post(f"{base}/subagents/sg-1/resume", headers=AUTH)
    assert started.status_code == 201
    assert started.json() == {"job_id": "job-7"}
    assert seen == ["sg-1"]

    host.harness.deps.services.resume_subagent = None
    missing = test_client.post(f"{base}/subagents/sg-1/resume", headers=AUTH)
    assert missing.status_code == 409
    assert missing.json()["error"]["message"] == (
        "sub-agent resume is not available in this session"
    )
    assert test_client.post(f"{base}/subagents/sg-1/resume", headers=AUTH).status_code == 409
    assert (
        test_client.post(
            f"/v1/workspaces/{ws_id}/sessions/nope/subagents/sg-1/resume", headers=AUTH
        ).status_code
        == 404
    )


def _mount_idle_host(test_client, base: str) -> None:
    """Drive one real turn to a parked approval, answer it and poll back to
    idle so the supervisor holds a live host for ``base`` (``peek`` only
    answers with a live host, and the fixture's idle_ttl keeps it mounted)."""
    test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    state = _poll(test_client, base, lambda s: s["status"] == "waiting_ask")
    [ask] = state["pending_asks"]
    test_client.post(f"{base}/asks/{ask['id']}", headers=AUTH, json={"approve": True})
    _poll(test_client, base, lambda s: s["status"] == "idle")


def test_get_cache_control_headers(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"

    cases = [
        ("/v1/health", None, "no-cache"),
        ("/v1/models", AUTH, "max-age=300"),
        ("/v1/workspaces", AUTH, "max-age=60"),
        (f"/v1/workspaces/{ws_id}/sessions", AUTH, "max-age=60"),
        (base, AUTH, "no-cache"),
        (f"{base}/history", AUTH, "max-age=10"),
        (f"{base}/asks", AUTH, "no-cache"),
        (f"{base}/jobs", AUTH, "max-age=30"),
    ]
    for path, headers, expected in cases:
        response = test_client.get(path, headers=headers or {})
        assert response.status_code == 200, path
        assert response.headers.get("cache-control") == expected, path


def test_post_message_409s_when_another_process_claims_the_session(client):
    """A session open in the TUI must be refused, not silently co-hosted."""
    from marim_harness.session.claim import try_acquire
    from marim_harness.session.store import SessionManager

    test_client, tmp_path = client
    ws_id, session_id, project = _setup_workspace_and_session(test_client, tmp_path)
    session_path = SessionManager(project).session_path(session_id)
    outsider = try_acquire(session_path, kind="tui")
    assert outsider is not None
    try:
        response = test_client.post(
            f"/v1/workspaces/{ws_id}/sessions/{session_id}/messages",
            json={"prompt": "hi"},
            headers=AUTH,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "claimed"
        assert "tui" in response.json()["error"]["message"]
    finally:
        outsider.release()


def test_post_message_404s_when_session_vanishes_after_the_precheck(client, monkeypatch):
    """The delete/claim inode race (finding #3529): post_message's own
    _session_exists guard passed (the session existed when it ran), but the
    file is gone by the time host_for actually claims it — the same window a
    racing DELETE opens in production (POST checks _session_exists, a DELETE
    completes, then the claim lands on the DELETE's orphaned-then-recreated
    sidecar inode). SessionSupervisor._claim_session's post-claim existence
    check must turn this into 404, not silently build (and persist) an empty
    session under the deleted id. The precheck is stubbed to simulate landing
    inside the race window; a plain pre-deleted session would 404 via the
    precheck alone and never reach the code this test targets."""
    import marim_harness.server.http as http_mod
    from marim_harness.session.store import SessionManager

    test_client, tmp_path = client
    ws_id, session_id, project = _setup_workspace_and_session(test_client, tmp_path)
    session_path = SessionManager(project).session_path(session_id)
    session_path.unlink()

    monkeypatch.setattr(http_mod, "_session_exists", lambda record, sid: True)

    response = test_client.post(
        f"/v1/workspaces/{ws_id}/sessions/{session_id}/messages",
        json={"prompt": "hi"},
        headers=AUTH,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_delete_session_409s_when_another_process_claims_it(client):
    """A claimed session is being driven elsewhere; deletion must be refused."""
    from marim_harness.session.claim import try_acquire
    from marim_harness.session.store import SessionManager

    test_client, tmp_path = client
    ws_id, session_id, project = _setup_workspace_and_session(test_client, tmp_path)
    session_path = SessionManager(project).session_path(session_id)
    outsider = try_acquire(session_path, kind="tui")
    assert outsider is not None
    try:
        response = test_client.delete(f"/v1/workspaces/{ws_id}/sessions/{session_id}", headers=AUTH)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "claimed"
        assert "tui" in response.json()["error"]["message"]
        assert session_path.exists()  # nothing deleted while claimed
    finally:
        outsider.release()


def test_post_message_trigger_is_user_or_system_only(client):
    """Phase 4a: a client-side slash command's own prompt is posted with
    ``trigger: system`` (no user bubble on the transcript); ``autonomous`` is
    the daemon's own WakeDriver's and is refused at the schema."""
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path, mode="auto")
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    refused = test_client.post(
        f"{base}/messages", headers=AUTH, json={"prompt": "wake", "trigger": "autonomous"}
    )
    assert refused.status_code == 400
    accepted = test_client.post(
        f"{base}/messages", headers=AUTH, json={"prompt": "remember this", "trigger": "system"}
    )
    assert accepted.status_code == 202
    _poll(test_client, base, lambda d: d["status"] == "idle")
    history = test_client.get(f"{base}/history", headers=AUTH).json()
    assert history["message_count"] >= 1  # the system-triggered turn ran


def test_get_session_seeds_an_attaching_client(client):
    """Phase 4a: ``GET session`` carries what a TUI needs to seed its view
    before the first event. Cold (no host loaded) the live-only fields are
    absent or None; once the host is loaded they are authoritative."""
    test_client, tmp_path = client
    ws_id, sid, project = _setup_workspace_and_session(test_client, tmp_path, mode="auto")
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
    cold = test_client.get(base, headers=AUTH).json()
    assert cold["workspace_path"] == str(project)
    assert cold["usage"] is None and cold["compact_threshold"] is None
    assert cold["session"]["mode"] == "auto"  # the persisted value, from the listing
    assert "model_label" not in cold["session"]  # only a loaded host knows the label

    assert (
        test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "hi"}).status_code == 202
    )
    warm = _poll(test_client, base, lambda d: d["status"] == "idle" and d["usage"] is not None)
    live = warm["session"]
    assert live["mode"] == "auto"
    assert isinstance(live["model_label"], str) and live["model_label"]
    assert "advisor_model" in live and "thinking" in live
    assert warm["compact_threshold"] > 0
    assert {"input_tokens", "output_tokens"} <= set(warm["usage"])
    # The mode reported is the host's live one, not the persisted value.
    assert test_client.post(f"{base}/mode", headers=AUTH, json={"mode": "plan"}).status_code == 200
    assert test_client.get(base, headers=AUTH).json()["session"]["mode"] == "plan"

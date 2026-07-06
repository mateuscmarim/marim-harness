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

import json as _json
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

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
    "request_approval", "ask_user", "on_subagent_event", "on_subagent_notice",
    "on_subagent_model", "on_subagent_usage", "detach_fanout", "interactive",
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
    return Harness(model=model, provider=BuiltinToolProvider(), deps=deps,
                   instructions="You are a coding agent.", **config_kwargs)


def _edit_model() -> FunctionModel:
    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="read_file", args={"path": "a.txt"})]
            )
        if state["n"] == 2:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="edit_file",
                args={"path": "a.txt",
                      "edits": [{"old_string": "foo", "new_string": "bar"}]},
            )])
        return ModelResponse(parts=[TextPart(content="done")])

    stream_state = {"n": 0}

    async def stream_fn(messages, info):
        stream_state["n"] += 1
        if stream_state["n"] == 1:
            yield {0: DeltaToolCall(name="read_file",
                                    json_args=_json.dumps({"path": "a.txt"}),
                                    tool_call_id="tc-read-1")}
        elif stream_state["n"] == 2:
            yield {0: DeltaToolCall(
                name="edit_file",
                json_args=_json.dumps({"path": "a.txt",
                                       "edits": [{"old_string": "foo",
                                                  "new_string": "bar"}]}),
                tool_call_id="tc-edit-1")}
        else:
            yield "done"

    return FunctionModel(fn, stream_function=stream_fn)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

    async def factory(workspace: Path, session_id: str, mode):
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


def _setup_workspace_and_session(client, tmp_path, mode="ask"):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "a.txt").write_text("foo\n")
    ws = client.post("/v1/workspaces", headers=AUTH,
                     json={"name": "proj", "path": str(project)}).json()
    sid = client.post(f"/v1/workspaces/{ws['id']}/sessions", headers=AUTH,
                      json={"name": "run1", "mode": mode}).json()["id"]
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
    assert test_client.get("/v1/workspaces", headers={
        "Authorization": "Bearer wrong"}).status_code == 401


def test_workspace_crud(client):
    test_client, tmp_path = client
    project = tmp_path / "proj"
    project.mkdir()
    created = test_client.post("/v1/workspaces", headers=AUTH,
                               json={"name": "proj", "path": str(project)})
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
    missing = test_client.get(f"/v1/workspaces/{ws_id}/sessions/nope", headers=AUTH)
    assert missing.status_code == 404


def test_full_turn_with_parked_approval(client):
    test_client, tmp_path = client
    ws_id, sid, project = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"

    accepted = test_client.post(f"{base}/messages", headers=AUTH, json={"prompt": "edit it"})
    assert accepted.status_code == 202
    turn_id = accepted.json()["turn_id"]

    # The edit parks for approval; answer it over the API.
    state = _poll(test_client, base, lambda s: s["status"] == "waiting_ask")
    [ask] = state["pending_asks"]
    assert ask["payload"]["tool_name"] == "edit_file"
    answered = test_client.post(f"{base}/asks/{ask['id']}", headers=AUTH,
                                json={"approve": True})
    assert answered.status_code == 200

    _poll(test_client, base, lambda s: s["status"] == "idle")
    assert (project / "a.txt").read_text() == "bar\n"

    # Replay the whole stream via SSE and find the lifecycle events.
    events = []
    with test_client.stream("GET", f"{base}/events?access_token={TOKEN}",
                            headers={"Last-Event-ID": "0"}) as stream:
        for line in stream.iter_lines():
            if line.startswith("event: "):
                events.append(line.removeprefix("event: "))
            if line.startswith("data: ") and '"turn_id"' in line and "output" in line:
                payload = _json.loads(line.removeprefix("data: "))
                if payload.get("turn_id") == turn_id and "output" in payload:
                    break
    assert "turn.started" in events
    assert "ask.pending" in events
    assert "ask.resolved" in events
    assert "turn.finished" in events

    # History endpoint serves the persisted messages.
    history = test_client.get(f"{base}/history", headers=AUTH).json()
    assert history["message_count"] > 0
    assert len(history["messages"]) > 0


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
    assert test_client.post(f"{base}/steer", headers=AUTH,
                            json={"text": "hey"}).status_code == 200
    test_client.post(f"{base}/interrupt", headers=AUTH)


def test_unknown_workspace_and_session_404(client):
    test_client, _ = client
    assert test_client.get("/v1/workspaces/nope/sessions", headers=AUTH).status_code == 404
    assert test_client.post("/v1/workspaces/nope/sessions/x/messages", headers=AUTH,
                            json={"prompt": "hi"}).status_code == 404


def test_malformed_base64_attachment_returns_400(client):
    test_client, tmp_path = client
    ws_id, sid, _ = _setup_workspace_and_session(test_client, tmp_path)
    base = f"/v1/workspaces/{ws_id}/sessions/{sid}"

    # POST a message with an attachment containing malformed base64
    response = test_client.post(f"{base}/messages", headers=AUTH, json={
        "prompt": "process this",
        "attachments": [
            {"data_b64": "not-valid-base64!!!", "media_type": "image/png"}
        ]
    })
    assert response.status_code == 400
    error_body = response.json()
    assert error_body["error"]["code"] == "bad_request"
    assert "base64" in error_body["error"]["message"]

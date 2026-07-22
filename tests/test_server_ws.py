"""WebSocket event transport: auth, not-found, live/replay, resume."""

import json as _json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.server.http import create_app
from marim_harness.server.supervisor import SessionSupervisor
from marim_harness.server.workspaces import WorkspaceRegistry
from marim_harness.tools.provider import BuiltinToolProvider

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _reply_model() -> FunctionModel:
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="done")])

    async def stream_fn(messages, info):
        yield "done"

    return FunctionModel(fn, stream_function=stream_fn)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

    async def factory(workspace: Path, session_id: str, mode):
        from marim_harness.session import SessionManager

        manager = SessionManager(workspace)
        store = manager.store(session_id)
        deps = Deps(
            workspace=WorkspaceConfig(root=workspace, mode=mode or Mode.auto),
            ui=UIHooks(),
        )
        return Harness(model=_reply_model(), provider=BuiltinToolProvider(),
                       deps=deps, instructions="You are a coding agent.",
                       store=store, manager=manager)

    registry = WorkspaceRegistry(tmp_path / "state" / "workspaces.json",
                                 tmp_path / "managed")
    supervisor = SessionSupervisor(factory, idle_ttl=3600.0)
    return create_app(registry=registry, supervisor=supervisor, token=TOKEN), tmp_path


def _make_session(tc, tmp_path, mode="auto"):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    ws = tc.post("/v1/workspaces", headers=AUTH,
                 json={"name": "proj", "path": str(project)}).json()
    sid = tc.post(f"/v1/workspaces/{ws['id']}/sessions", headers=AUTH,
                  json={"name": "run1", "mode": mode}).json()["id"]
    return ws["id"], sid


def _poll_idle(tc, base):
    import time
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if tc.get(base, headers=AUTH).json().get("status") == "idle":
            return
        time.sleep(0.02)
    raise AssertionError("session never reached idle")


def test_ws_rejects_missing_token(app):
    application, tmp_path = app
    with TestClient(application) as tc:
        ws_id, sid = _make_session(tc, tmp_path)
        with pytest.raises(WebSocketDisconnect) as exc:
            with tc.websocket_connect(
                f"/v1/workspaces/{ws_id}/sessions/{sid}/ws"
            ) as socket:
                socket.receive_text()
        assert exc.value.code == 4401


def test_ws_unknown_session_closes_4404(app):
    application, tmp_path = app
    with TestClient(application) as tc:
        project = tmp_path / "proj"
        project.mkdir(exist_ok=True)
        ws = tc.post("/v1/workspaces", headers=AUTH,
                     json={"name": "proj", "path": str(project)}).json()
        with pytest.raises(WebSocketDisconnect) as exc:
            with tc.websocket_connect(
                f"/v1/workspaces/{ws['id']}/sessions/nope/ws", headers=AUTH
            ) as socket:
                socket.receive_text()
        assert exc.value.code == 4404


def test_ws_replays_turn_lifecycle(app):
    application, tmp_path = app
    with TestClient(application) as tc:
        ws_id, sid = _make_session(tc, tmp_path)
        base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
        assert tc.post(f"{base}/messages", headers=AUTH,
                       json={"prompt": "hi"}).status_code == 202
        _poll_idle(tc, base)
        # after_seq=0 replays the whole ring for the completed turn.
        types = []
        with tc.websocket_connect(f"{base}/ws?after_seq=0", headers=AUTH) as socket:
            while True:
                event = socket.receive_json()
                types.append(event["type"])
                assert set(event) == {"seq", "ts", "type", "data"}
                if event["type"] == "turn.finished":
                    break
        assert "turn.started" in types
        assert "turn.finished" in types


def test_ws_resume_after_seq_skips_replayed(app):
    application, tmp_path = app
    with TestClient(application) as tc:
        ws_id, sid = _make_session(tc, tmp_path)
        base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
        tc.post(f"{base}/messages", headers=AUTH, json={"prompt": "go"})
        _poll_idle(tc, base)
        with tc.websocket_connect(f"{base}/ws?after_seq=0", headers=AUTH) as socket:
            first_seq = socket.receive_json()["seq"]
        with tc.websocket_connect(
            f"{base}/ws?after_seq={first_seq}", headers=AUTH
        ) as socket:
            assert socket.receive_json()["seq"] == first_seq + 1

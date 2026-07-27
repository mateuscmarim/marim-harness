"""Workspace-level trust endpoints (``GET``/``POST .../trust``) and the
``trust_prompt_pending`` session-payload field.

Follows the ``TestClient``-over-Starlette pattern used by
``tests/test_server_ws.py`` (plain request/response endpoints — no streaming
involved, so the real-uvicorn workaround ``test_server_http.py`` needs for its
SSE test doesn't apply here). Every test sets ``XDG_STATE_HOME`` (the trust
store lives there) and clears ``MARIM_TRUST_PROJECT_HOOKS`` so xdist-parallel
runs never see another worker's env leak in or a real operator override."""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from starlette.testclient import TestClient

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
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)

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


def _mk_project_with_skill(tmp_path, name="proj") -> Path:
    project = tmp_path / name
    project.mkdir(exist_ok=True)
    skill_dir = project / ".marim" / "skills" / "deploy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: deploy\ndescription: x\n---\n")
    return project


def _register_workspace(tc, project: Path, name="proj") -> str:
    return tc.post("/v1/workspaces", headers=AUTH,
                   json={"name": name, "path": str(project)}).json()["id"]


def _poll_idle(tc, base, timeout=10.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tc.get(base, headers=AUTH).json()["status"] == "idle":
            return
        time.sleep(0.02)
    raise AssertionError("session never reached idle")


def test_get_trust_untrusted_with_gated_surface(app):
    application, tmp_path = app
    project = _mk_project_with_skill(tmp_path)
    with TestClient(application) as tc:
        ws_id = _register_workspace(tc, project)
        resp = tc.get(f"/v1/workspaces/{ws_id}/trust", headers=AUTH)
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-cache"
        body = resp.json()
        assert body["trusted"] is False
        assert body["source"] == "default"
        assert body["fingerprint_fresh"] is False
        assert body["surface"]["skills"] == ["deploy"]
        assert "skills: 1" in body["surface"]["summary"]


def test_get_trust_unknown_workspace_404(app):
    application, _ = app
    with TestClient(application) as tc:
        resp = tc.get("/v1/workspaces/nope/trust", headers=AUTH)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


def test_get_trust_requires_auth(app):
    application, tmp_path = app
    project = _mk_project_with_skill(tmp_path)
    with TestClient(application) as tc:
        ws_id = _register_workspace(tc, project)
        assert tc.get(f"/v1/workspaces/{ws_id}/trust").status_code == 401


def test_post_trust_grant_persists_and_get_reflects_store(app):
    application, tmp_path = app
    project = _mk_project_with_skill(tmp_path)
    with TestClient(application) as tc:
        ws_id = _register_workspace(tc, project)

        granted = tc.post(f"/v1/workspaces/{ws_id}/trust", headers=AUTH,
                          json={"trusted": True})
        assert granted.status_code == 200
        body = granted.json()
        assert body == {"trusted": True, "applied_sessions": 0, "restart_note": None}

        from marim_harness.trust import stored_decision

        stored = stored_decision(project)
        assert stored is not None
        assert stored.trusted is True

        after = tc.get(f"/v1/workspaces/{ws_id}/trust", headers=AUTH).json()
        assert after["trusted"] is True
        assert after["source"] == "store"
        assert after["fingerprint_fresh"] is True


def test_post_trust_grant_hot_applies_to_live_session(app):
    application, tmp_path = app
    project = _mk_project_with_skill(tmp_path)
    with TestClient(application) as tc:
        ws_id = _register_workspace(tc, project)
        sid = tc.post(f"/v1/workspaces/{ws_id}/sessions", headers=AUTH,
                      json={"name": "run1", "mode": "auto"}).json()["id"]
        base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
        # Mount a live host by driving one turn to completion.
        assert tc.post(f"{base}/messages", headers=AUTH,
                       json={"prompt": "hi"}).status_code == 202
        _poll_idle(tc, base)

        granted = tc.post(f"/v1/workspaces/{ws_id}/trust", headers=AUTH,
                          json={"trusted": True})
        assert granted.status_code == 200
        assert granted.json()["applied_sessions"] == 1
        assert granted.json()["restart_note"] is None

        host = application.state.supervisor.peek(ws_id, sid)
        assert host is not None
        assert host.harness.deps.trust.project is True


def test_post_trust_revoke_reports_restart_note_for_live_session(app):
    application, tmp_path = app
    project = _mk_project_with_skill(tmp_path)
    with TestClient(application) as tc:
        ws_id = _register_workspace(tc, project)
        sid = tc.post(f"/v1/workspaces/{ws_id}/sessions", headers=AUTH,
                      json={"name": "run1", "mode": "auto"}).json()["id"]
        base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
        tc.post(f"{base}/messages", headers=AUTH, json={"prompt": "hi"})
        _poll_idle(tc, base)

        tc.post(f"/v1/workspaces/{ws_id}/trust", headers=AUTH, json={"trusted": True})
        revoked = tc.post(f"/v1/workspaces/{ws_id}/trust", headers=AUTH,
                          json={"trusted": False})
        assert revoked.status_code == 200
        body = revoked.json()
        assert body["trusted"] is False
        assert body["applied_sessions"] == 1
        assert body["restart_note"] is not None

        from marim_harness.trust import stored_decision

        assert stored_decision(project).trusted is False
        host = application.state.supervisor.peek(ws_id, sid)
        assert host.harness.deps.trust.project is False


def test_post_trust_revoke_no_live_sessions_has_no_restart_note(app):
    application, tmp_path = app
    project = _mk_project_with_skill(tmp_path)
    with TestClient(application) as tc:
        ws_id = _register_workspace(tc, project)
        revoked = tc.post(f"/v1/workspaces/{ws_id}/trust", headers=AUTH,
                          json={"trusted": False})
        assert revoked.status_code == 200
        body = revoked.json()
        assert body["applied_sessions"] == 0
        assert body["restart_note"] is None


def test_post_trust_malformed_body_400(app):
    application, tmp_path = app
    project = _mk_project_with_skill(tmp_path)
    with TestClient(application) as tc:
        ws_id = _register_workspace(tc, project)
        resp = tc.post(f"/v1/workspaces/{ws_id}/trust", headers=AUTH, json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "bad_request"


def test_post_trust_unknown_workspace_404(app):
    application, _ = app
    with TestClient(application) as tc:
        resp = tc.post("/v1/workspaces/nope/trust", headers=AUTH, json={"trusted": True})
        assert resp.status_code == 404


def test_session_payload_includes_trust_prompt_pending(app):
    application, tmp_path = app
    project = _mk_project_with_skill(tmp_path)
    with TestClient(application) as tc:
        ws_id = _register_workspace(tc, project)

        created = tc.post(f"/v1/workspaces/{ws_id}/sessions", headers=AUTH,
                          json={"name": "run1"})
        assert created.json()["trust_prompt_pending"] is True
        sid = created.json()["id"]

        detail = tc.get(f"/v1/workspaces/{ws_id}/sessions/{sid}", headers=AUTH).json()
        assert detail["trust_prompt_pending"] is True

        rows = tc.get(f"/v1/workspaces/{ws_id}/sessions", headers=AUTH).json()["sessions"]
        [row] = [r for r in rows if r["id"] == sid]
        assert row["trust_prompt_pending"] is True

        # Grant trust; the surface's fingerprint hasn't changed, so a fresh
        # session-create/detail/list call — even before any host mounts —
        # must all agree the prompt is no longer owed.
        tc.post(f"/v1/workspaces/{ws_id}/trust", headers=AUTH, json={"trusted": True})

        created2 = tc.post(f"/v1/workspaces/{ws_id}/sessions", headers=AUTH,
                           json={"name": "run2"})
        assert created2.json()["trust_prompt_pending"] is False
        detail2 = tc.get(f"/v1/workspaces/{ws_id}/sessions/{sid}", headers=AUTH).json()
        assert detail2["trust_prompt_pending"] is False


def test_session_payload_pending_false_for_empty_surface(app):
    """An empty gated surface never needs a prompt, regardless of decision."""
    application, tmp_path = app
    project = tmp_path / "plain"
    project.mkdir()
    with TestClient(application) as tc:
        ws_id = _register_workspace(tc, project, name="plain")
        created = tc.post(f"/v1/workspaces/{ws_id}/sessions", headers=AUTH,
                          json={"name": "run1"})
        assert created.json()["trust_prompt_pending"] is False


def test_grant_persist_failure_still_flips_state_and_500s(app, monkeypatch):
    """A store-write OSError must return 500 trust_store_error while the live
    TrustState still flips (the user consented; only durability failed)."""
    application, tmp_path = app
    project = _mk_project_with_skill(tmp_path)
    with TestClient(application) as tc:
        ws_id = _register_workspace(tc, project)
        sid = tc.post(f"/v1/workspaces/{ws_id}/sessions", headers=AUTH,
                      json={"name": "run1", "mode": "auto"}).json()["id"]
        base = f"/v1/workspaces/{ws_id}/sessions/{sid}"
        # Mount a live host by driving one turn to completion.
        assert tc.post(f"{base}/messages", headers=AUTH,
                       json={"prompt": "hi"}).status_code == 202
        _poll_idle(tc, base)

        # Monkeypatch record_decision to raise OSError.
        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("marim_harness.server.http.record_decision", boom)

        # POST trust grant; the store write fails but the state flips live.
        resp = tc.post(f"/v1/workspaces/{ws_id}/trust", headers=AUTH,
                       json={"trusted": True})
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"]["code"] == "trust_store_error"
        assert "disk full" in body["error"]["message"]

        # The live host's state flipped despite the failed write.
        host = application.state.supervisor.peek(ws_id, sid)
        assert host is not None
        assert host.harness.deps.trust.project is True

        # The store on disk has NO decision recorded.
        from marim_harness.trust import stored_decision

        assert stored_decision(project) is None

"""Chat workspaces are distinct managed, purgeable sandboxes."""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from marim_harness.server.http import create_app
from marim_harness.server.schema import WorkspaceIn
from marim_harness.server.supervisor import SessionSupervisor
from marim_harness.server.workspaces import WorkspaceRegistry

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_registry(tmp_path: Path) -> WorkspaceRegistry:
    return WorkspaceRegistry(tmp_path / "workspaces.json", tmp_path / "ws")


def test_workspace_in_defaults_chat_false_and_ignores_unknown_fields():
    body = WorkspaceIn.model_validate({"name": "x"})
    assert body.chat is False
    future_body = WorkspaceIn.model_validate({"name": "x", "future_field": 1})
    assert future_body.chat is False


def test_create_with_chat_true_records_chat_kind(tmp_path: Path):
    registry = make_registry(tmp_path)
    record = registry.create_managed("chat-00000001", chat=True)
    assert record.kind == "chat"
    assert (tmp_path / "ws" / record.id).is_dir()
    assert json.loads(json.dumps(record.as_dict()))["kind"] == "chat"


def test_create_without_chat_stays_managed(tmp_path: Path):
    registry = make_registry(tmp_path)
    assert registry.create_managed("proj").kind == "managed"


def test_http_create_forwards_chat_flag_to_registry(tmp_path: Path):
    registry = make_registry(tmp_path)
    app = create_app(registry=registry, supervisor=SessionSupervisor(), token=TOKEN)

    with TestClient(app) as client:
        response = client.post(
            "/v1/workspaces",
            headers=AUTH,
            json={"name": "chat-00000001", "chat": True},
        )

    assert response.status_code == 201
    assert response.json()["kind"] == "chat"
    assert registry.get(response.json()["id"]).kind == "chat"


def test_purge_allowed_for_chat_and_managed_but_not_registered(tmp_path: Path):
    registry = make_registry(tmp_path)
    chat = registry.create_managed("chat-00000001", chat=True)
    registry.delete(chat.id, purge=True)
    managed = registry.create_managed("m")
    registry.delete(managed.id, purge=True)
    registered_root = tmp_path / "registered"
    registered_root.mkdir()
    registered = registry.register("r", registered_root)
    with pytest.raises(ValueError):
        registry.delete(registered.id, purge=True)

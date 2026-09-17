"""The health version describes this app instance, not subsequently installed metadata."""

from importlib.metadata import PackageNotFoundError
from unittest.mock import Mock

from starlette.testclient import TestClient

from marim_harness.server.http import create_app
from marim_harness.server.workspaces import WorkspaceRegistry


def _app(tmp_path):
    registry = WorkspaceRegistry(tmp_path / "workspaces.json", tmp_path / "workspaces")
    return create_app(registry=registry, supervisor=Mock(), token="test-token")


def test_health_version_contract(monkeypatch, tmp_path):
    def version(distribution):
        assert distribution == "marim-harness"
        return "0.9.1"

    monkeypatch.setattr("importlib.metadata.version", version)
    response = TestClient(_app(tmp_path)).get("/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == "0.9.1"
    assert response.headers["cache-control"] == "no-cache"


def test_health_version_missing_metadata(monkeypatch, tmp_path):
    def unavailable(distribution):
        raise PackageNotFoundError(distribution)

    monkeypatch.setattr("importlib.metadata.version", unavailable)
    response = TestClient(_app(tmp_path)).get("/v1/health")
    assert response.status_code == 200
    assert response.json()["version"] is None


def test_health_version_startup_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr("importlib.metadata.version", lambda _: "0.9.1")
    client = TestClient(_app(tmp_path))
    monkeypatch.setattr("importlib.metadata.version", lambda _: "0.10.0")
    assert client.get("/v1/health").json()["version"] == "0.9.1"
    assert TestClient(_app(tmp_path)).get("/v1/health").json()["version"] == "0.10.0"

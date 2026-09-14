"""Workspace file downloads stay authenticated, bounded and inside the workspace."""

import asyncio
import os
import socket
import threading
from urllib.parse import quote

import pytest
from pydantic_ai.usage import RunUsage
from starlette.requests import ClientDisconnect, Request
from starlette.testclient import TestClient

from marim_harness.server.http import create_app
from marim_harness.server.supervisor import SessionSupervisor
from marim_harness.server.workspaces import WorkspaceRegistry
from marim_harness.session import SessionManager

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def downloads(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    registry = WorkspaceRegistry(tmp_path / "registry.json", tmp_path / "managed")
    record = registry.register("test", root)
    store = SessionManager(root).create("test")
    store.save([], RunUsage())
    supervisor = SessionSupervisor()
    app = create_app(registry=registry, supervisor=supervisor, token="test-token")
    base = f"/v1/workspaces/{record.id}/sessions/{store.session_id}/files"
    with TestClient(app) as client:
        yield client, root, base


def test_downloads_workspace_file_with_private_headers(downloads):
    client, root, base = downloads
    (root / "report.pdf").write_bytes(b"%PDF-example")
    response = client.get(base, params={"path": "report.pdf"}, headers=AUTH)
    assert response.status_code == 200
    assert response.content == b"%PDF-example"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-length"] == "12"
    assert response.headers["cache-control"] == "private, no-store"
    assert "report.pdf" in response.headers["content-disposition"]


@pytest.mark.parametrize("absolute", [False, True])
def test_spaced_unicode_nested_paths(downloads, absolute):
    client, root, base = downloads
    target = root / "my reports" / "olá 世界.txt"
    target.parent.mkdir()
    target.write_bytes(b"hello")
    path = str(target if absolute else target.relative_to(root))
    response = client.get(base, params={"path": path}, headers=AUTH)
    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers["content-type"] == "text/plain"
    assert response.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''" + quote(target.name, safe="")
    )


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_files_require_authentication(downloads, headers):
    client, root, base = downloads
    (root / "file.txt").write_text("private")
    response = client.get(base, params={"path": "file.txt"}, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_files_require_matching_workspace_and_session(downloads):
    client, root, base = downloads
    (root / "file.txt").write_text("private")
    invalid_scopes = [
        base.replace("/workspaces/test/", "/workspaces/unknown/"),
        base.rsplit("/", 2)[0] + "/unknown/files",
    ]
    other = root.parent / "other"
    other.mkdir()
    registered = client.post(
        "/v1/workspaces", json={"name": "other", "path": str(other)}, headers=AUTH
    ).json()
    invalid_scopes.append(base.replace("/workspaces/test/", f"/workspaces/{registered['id']}/"))
    for url in invalid_scopes:
        response = client.get(url, params={"path": "file.txt"}, headers=AUTH)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("path", [None, "", "  ", "bad\x00path", "bad\npath"])
def test_invalid_path_is_bad_request(downloads, path):
    client, _, base = downloads
    response = client.get(base, params={} if path is None else {"path": path}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_multiple_paths_are_bad_request(downloads):
    client, _, base = downloads
    response = client.get(base, params=[("path", "a"), ("path", "b")], headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


@pytest.mark.parametrize("kind", ["missing", "outside", "traversal", "directory", "fifo", "socket"])
def test_unavailable_paths_have_indistinguishable_sanitized_errors(downloads, kind, caplog):
    client, root, base = downloads
    outside = root.parent / "secret.txt"
    outside.write_text("private")
    paths = {"missing": "missing", "outside": str(outside), "traversal": "../secret.txt"}
    paths["directory"] = str(root)
    os.mkfifo(root / "fifo")
    paths["fifo"] = "fifo"
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(root / "socket"))
        paths["socket"] = "socket"
        caplog.set_level("DEBUG", logger="marim_harness.server")
        response = client.get(base, params={"path": paths[kind]}, headers=AUTH)
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "file is unavailable in this workspace"}
    }
    assert str(root.parent) not in response.text
    assert str(root.parent) not in "\n".join(
        record.getMessage() for record in caplog.records
        if record.name.startswith("marim_harness.server")
    )


@pytest.mark.parametrize("target_inside", [False, True])
@pytest.mark.parametrize("directory", [False, True])
def test_symlinks_are_rejected_even_for_internal_targets(downloads, target_inside, directory):
    client, root, base = downloads
    target_root = root if target_inside else root.parent
    target = target_root / "target"
    target.mkdir()
    (target / "file.txt").write_text("contents")
    link = root / "link"
    link.symlink_to(target if directory else target / "file.txt")
    response = client.get(
        base, params={"path": "link/file.txt" if directory else "link"}, headers=AUTH
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_size_limit_is_checked_before_streaming(downloads):
    from marim_harness.server.files import MAX_FILE_BYTES

    client, root, base = downloads
    with (root / "large.bin").open("wb") as stream:
        stream.truncate(MAX_FILE_BYTES + 1)
    response = client.get(base, params={"path": "large.bin"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert "50 MiB" in response.json()["error"]["message"]


def test_unknown_mime_and_empty_file(downloads):
    client, root, base = downloads
    (root / "empty.marim-unknown").touch()
    response = client.get(base, params={"path": "empty.marim-unknown"}, headers=AUTH)
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "0"
    assert response.headers["content-type"] == "application/octet-stream"


def test_response_closes_open_file_on_success(downloads, monkeypatch):
    from marim_harness.server import http

    client, root, base = downloads
    (root / "file.txt").write_text("hello")
    opened = []
    original = http.open_workspace_file

    def capture(*args):
        download = original(*args)
        opened.append(download)
        return download

    monkeypatch.setattr(http, "open_workspace_file", capture)
    assert client.get(base, params={"path": "file.txt"}, headers=AUTH).status_code == 200
    assert len(opened) == 1
    assert opened[0].stream.closed


@pytest.mark.anyio
@pytest.mark.parametrize("disconnect", ["send_failure", "early_disconnect", "cancelled"])
async def test_response_closes_file_on_disconnect(tmp_path, disconnect):
    from marim_harness.server.files import open_workspace_file
    from marim_harness.server.http import _WorkspaceFileResponse

    (tmp_path / "file.txt").write_text("hello")
    download = open_workspace_file(tmp_path, "file.txt")
    response = _WorkspaceFileResponse(download)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if disconnect == "send_failure":
            raise OSError("client left")
        if disconnect == "cancelled":
            raise asyncio.CancelledError
        await asyncio.sleep(10)

    scope = {"type": "http", "asgi": {"spec_version": "2.0"}}
    if disconnect == "early_disconnect":
        await response(scope, receive, send)
    else:
        scope["asgi"]["spec_version"] = "2.4"
        expected = ClientDisconnect if disconnect == "send_failure" else asyncio.CancelledError
        with pytest.raises(expected):
            await response(scope, receive, send)
    assert download.stream.closed


@pytest.mark.anyio
async def test_cancelled_file_open_closes_late_descriptor(downloads, monkeypatch):
    from marim_harness.server import http

    client, root, base = downloads
    (root / "file.txt").write_text("hello")
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    opened = []
    original = http.open_workspace_file

    def delayed_open(*args):
        started.set()
        assert release.wait(2)
        download = original(*args)
        opened.append(download)
        original_close = download.close

        def close():
            original_close()
            closed.set()

        download.close = close
        return download

    monkeypatch.setattr(http, "open_workspace_file", delayed_open)
    request = Request({
        "type": "http", "app": client.app,
        "headers": [(b"authorization", b"Bearer test-token")],
        "query_string": b"path=file.txt",
        "path_params": {"ws": "test", "sid": base.split("/")[-2]},
    })
    task = asyncio.create_task(http.get_session_file(request))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(closed.wait, 2)
    finally:
        release.set()
        for download in opened:
            download.close()


def test_replacing_file_with_symlink_during_open_cannot_escape(tmp_path, monkeypatch):
    from marim_harness.server.files import WorkspaceFileNotFound, open_workspace_file

    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    target = root / "file.txt"
    target.write_text("public")
    original = os.open

    def replace_before_open(path, flags, *args, **kwargs):
        if path == "file.txt":
            target.unlink()
            target.symlink_to(outside)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    with pytest.raises(WorkspaceFileNotFound):
        open_workspace_file(root, "file.txt")


def test_replaced_parent_is_read_through_open_descriptor(tmp_path, monkeypatch):
    from marim_harness.server.files import open_workspace_file

    root = tmp_path / "workspace"
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()
    (nested / "file.txt").write_text("public")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("private")
    original = os.open

    def replace_after_open(path, flags, *args, **kwargs):
        descriptor = original(path, flags, *args, **kwargs)
        if path == "nested":
            nested.rename(root / "renamed")
            nested.symlink_to(outside)
        return descriptor

    monkeypatch.setattr(os, "open", replace_after_open)
    download = open_workspace_file(root, "nested/file.txt")
    try:
        assert b"".join(download.chunks()) == b"public"
    finally:
        download.close()


def test_growing_file_is_bounded_to_original_length_and_chunked(tmp_path):
    from marim_harness.server.files import open_workspace_file

    target = tmp_path / "file.txt"
    content = b"a" * (256 * 1024)
    target.write_bytes(content)
    download = open_workspace_file(tmp_path, "file.txt")
    with target.open("ab") as stream:
        stream.write(b"added later")
    try:
        chunks = list(download.chunks())
        assert len(chunks) > 1
        assert b"".join(chunks) == content
    finally:
        download.close()


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory", "oversize", "stat_failure"])
def test_failed_open_releases_every_descriptor(tmp_path, monkeypatch, kind):
    from marim_harness.server.files import (
        MAX_FILE_BYTES,
        InvalidFileRequest,
        WorkspaceFileNotFound,
        open_workspace_file,
    )

    target = tmp_path / "target"
    if kind == "symlink":
        target.symlink_to(tmp_path / "missing")
    elif kind == "directory":
        target.mkdir()
    elif kind in {"oversize", "stat_failure"}:
        with target.open("wb") as stream:
            stream.truncate(MAX_FILE_BYTES + 1)
    opened = []
    original_open, original_stat = os.open, os.fstat

    def capture(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail_stat(descriptor):
        raise OSError("stat failed")

    monkeypatch.setattr(os, "open", capture)
    if kind == "stat_failure":
        monkeypatch.setattr(os, "fstat", fail_stat)
    expected = InvalidFileRequest if kind == "oversize" else WorkspaceFileNotFound
    with pytest.raises(expected):
        open_workspace_file(tmp_path, "target")
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError):
            original_stat(descriptor)


def test_exact_size_limit_can_be_opened(tmp_path):
    from marim_harness.server.files import MAX_FILE_BYTES, open_workspace_file

    with (tmp_path / "file.bin").open("wb") as stream:
        stream.truncate(MAX_FILE_BYTES)
    download = open_workspace_file(tmp_path, "file.bin")
    try:
        assert download.size == MAX_FILE_BYTES
    finally:
        download.close()

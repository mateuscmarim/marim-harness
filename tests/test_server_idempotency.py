"""Opt-in mutation aliases deduplicate effects and persist replayable outcomes."""

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from marim_harness.server import http
from marim_harness.server.http import create_app
from marim_harness.server.idempotency import OperationStore, StoredResponse, request_fingerprint
from marim_harness.server.supervisor import SessionSupervisor
from marim_harness.server.workspaces import WorkspaceRegistry
from marim_harness.session import SessionManager
from tests.test_server_supervisor import _make_deps, _make_harness

AUTH = {"Authorization": "Bearer test-token"}
PROTECTED = "/v1/idempotent/workspaces"


def make_app(tmp_path, factory=None):
    async def unused_factory(*args, **kwargs):
        raise AssertionError("this test must not start a harness")

    registry = WorkspaceRegistry(tmp_path / "state/workspaces.json", tmp_path / "managed")
    return create_app(
        registry=registry,
        supervisor=SessionSupervisor(factory or unused_factory),
        token="test-token",
    )


def test_repeated_workspace_creation_replays_one_effect(tmp_path):
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    with TestClient(make_app(tmp_path)) as client:
        first = client.post(PROTECTED, json={"name": "one"}, headers=headers)
        second = client.post(PROTECTED, json={"name": "one"}, headers=headers)
        assert first.status_code == 201
        assert second.status_code == first.status_code
        assert second.content == first.content
        assert second.headers["Idempotency-Replayed"] == "true"
        assert first.headers["X-Marim-Idempotency"] == "v1"
        assert first.headers["Idempotency-Status"] == "completed"
        assert second.headers["Idempotency-Status"] == "completed"
        assert first.headers["Idempotency-Key"] == headers["Idempotency-Key"]
        listed = client.get("/v1/workspaces", headers=AUTH)
        assert len(listed.json()["workspaces"]) == 1
        assert listed.headers["X-Marim-Idempotency"] == "v1"


def test_all_existing_mutations_have_protected_aliases(tmp_path):
    app = make_app(tmp_path)
    mutations = {
        (route.path, tuple(sorted(route.methods)))
        for route in app.routes
        if isinstance(route, Route) and route.methods and route.methods <= {"POST", "DELETE"}
    }
    legacy = {(p, methods) for p, methods in mutations if p.startswith("/v1/workspaces")}
    assert len(legacy) == 13
    assert mutations - legacy == {
        (p.replace("/v1/", "/v1/idempotent/", 1), methods) for p, methods in legacy
    }


@pytest.mark.parametrize(
    "key", [None, "", "abc", "a" * 1000, "4EA1B17F-6CDF-4E71-9B62-9DB82D4F4E5D"]
)
def test_invalid_key_does_not_execute(tmp_path, key):
    headers = dict(AUTH)
    if key is not None:
        headers["Idempotency-Key"] = key
    with TestClient(make_app(tmp_path)) as client:
        response = client.post(PROTECTED, json={"name": "one"}, headers=headers)
        assert response.status_code == 400
        assert "Idempotency-Status" not in response.headers
        assert response.headers["X-Marim-Idempotency"] == "v1"
        assert client.get("/v1/workspaces", headers=AUTH).json() == {"workspaces": []}


def test_duplicate_key_headers_are_rejected(tmp_path):
    headers = [*AUTH.items(), ("Idempotency-Key", str(uuid4())), ("Idempotency-Key", str(uuid4()))]
    with TestClient(make_app(tmp_path)) as client:
        assert client.post(PROTECTED, json={"name": "one"}, headers=headers).status_code == 400


@pytest.mark.parametrize(
    "path,body",
    [
        (PROTECTED, b'{"name":"two"}'),
        (PROTECTED + "?purge=true", b'{"name":"one"}'),
        (PROTECTED + "/one/sessions", b'{"name":"one"}'),
    ],
)
def test_key_reuse_with_changed_target_query_or_body_conflicts(tmp_path, path, body):
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    with TestClient(make_app(tmp_path)) as client:
        assert client.post(PROTECTED, content=b'{"name":"one"}', headers=headers).status_code == 201
        response = client.post(path, content=body, headers=headers)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "idempotency_conflict"
        assert "Idempotency-Status" not in response.headers


def test_auth_is_rechecked_on_replay_and_token_rotation(tmp_path):
    app = make_app(tmp_path)
    key = str(uuid4())
    with TestClient(app) as client:
        first = client.post(
            PROTECTED, json={"name": "one"}, headers={**AUTH, "Idempotency-Key": key}
        )
        app.state.token = "rotated"
        denied = client.post(
            PROTECTED, json={"name": "one"}, headers={**AUTH, "Idempotency-Key": key}
        )
        assert denied.status_code == 401
        assert "Idempotency-Status" not in denied.headers
        replayed = client.post(
            PROTECTED,
            json={"name": "one"},
            headers={"Authorization": "Bearer rotated", "Idempotency-Key": key},
        )
        assert replayed.content == first.content
        assert replayed.headers["Idempotency-Replayed"] == "true"


@pytest.mark.anyio
async def test_unauthenticated_request_never_reads_body_or_creates_ledger(tmp_path):
    app = make_app(tmp_path)
    messages = []

    async def receive():
        raise AssertionError("unauthenticated body was read")

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": PROTECTED,
            "headers": [],
            "query_string": b"",
            "scheme": "http",
        },
        receive,
        send,
    )
    assert messages[0]["status"] == 401
    assert not (tmp_path / "state/idempotency").exists()


def test_completed_results_survive_app_restart(tmp_path):
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    with TestClient(make_app(tmp_path)) as client:
        first = client.post(PROTECTED, json={"name": "one"}, headers=headers)
    with TestClient(make_app(tmp_path)) as client:
        replayed = client.post(PROTECTED, json={"name": "one"}, headers=headers)
        assert replayed.status_code == 201
        assert replayed.content == first.content
        assert replayed.headers["Idempotency-Replayed"] == "true"
        assert len(client.get("/v1/workspaces", headers=AUTH).json()["workspaces"]) == 1
    ledger = tmp_path / "state/idempotency/operations.sqlite3"
    assert ledger.stat().st_mode & 0o777 == 0o600
    assert ledger.parent.stat().st_mode & 0o777 == 0o700
    assert b"test-token" not in ledger.read_bytes()


def test_session_create_settings_and_delete_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    with TestClient(make_app(tmp_path)) as client:
        client.post("/v1/workspaces", json={"name": "one"}, headers=AUTH).raise_for_status()
        headers = {**AUTH, "Idempotency-Key": str(uuid4())}
        path = PROTECTED + "/one/sessions"
        first = client.post(path, json={"name": "test"}, headers=headers)
        assert first.status_code == 201
        assert client.post(path, json={"name": "test"}, headers=headers).content == first.content
        sid = first.json()["id"]
        assert len(client.get("/v1/workspaces/one/sessions", headers=AUTH).json()["sessions"]) == 1
        for suffix, body in [("model", {"model": "local:test"}), ("mode", {"mode": "plan"})]:
            headers = {**AUTH, "Idempotency-Key": str(uuid4())}
            setting = f"{path}/{sid}/{suffix}"
            first = client.post(setting, json=body, headers=headers)
            assert first.status_code == 200
            assert client.post(setting, json=body, headers=headers).content == first.content
        for target in [f"{path}/{sid}", PROTECTED + "/one?purge=true"]:
            headers = {**AUTH, "Idempotency-Key": str(uuid4())}
            first = client.delete(target, headers=headers)
            assert first.status_code == 200
            replayed = client.delete(target, headers=headers)
            assert replayed.content == first.content
            assert replayed.headers["Idempotency-Replayed"] == "true"


def test_terminal_rejections_are_cached(tmp_path):
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    with TestClient(make_app(tmp_path)) as client:
        first = client.post(PROTECTED, json={}, headers=headers)
        second = client.post(PROTECTED, json={}, headers=headers)
        assert first.status_code == second.status_code == 400
        assert second.content == first.content
        assert second.headers["Idempotency-Replayed"] == "true"
        assert first.headers["Idempotency-Status"] == "completed"


def test_legacy_requests_keep_existing_behavior(tmp_path):
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    with TestClient(make_app(tmp_path)) as client:
        first = client.post("/v1/workspaces", json={"name": "one"}, headers=headers)
        second = client.post("/v1/workspaces", json={"name": "one"}, headers=headers)
        assert first.json()["id"] != second.json()["id"]
        assert "Idempotency-Status" not in first.headers


def test_claim_storage_failure_prevents_mutation(tmp_path, monkeypatch):
    app = make_app(tmp_path)

    def fail(*args):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(app.state.operation_store, "_connect", fail)
    with TestClient(app) as client:
        response = client.post(
            PROTECTED, json={"name": "one"}, headers={**AUTH, "Idempotency-Key": str(uuid4())}
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "idempotency_unavailable"
        assert response.headers["X-Marim-Idempotency"] == "v1"
        assert "Idempotency-Status" not in response.headers
        assert client.get("/v1/workspaces", headers=AUTH).json() == {"workspaces": []}


def _fail_sqlite_commit(monkeypatch, *, commit_number, after_commit):
    """Inject an ambiguous I/O result at an actual SQLite transaction boundary."""
    real_connect = sqlite3.connect
    commits = 0

    class FaultConnection(sqlite3.Connection):
        def commit(self):
            nonlocal commits
            commits += 1
            if commits != commit_number:
                return super().commit()
            if after_commit:
                super().commit()
            raise sqlite3.OperationalError("injected commit failure")

    def connect(*args, **kwargs):
        return real_connect(*args, factory=FaultConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)


@pytest.mark.parametrize("after_commit", [False, True])
def test_claim_commit_failure_never_starts_handler(tmp_path, monkeypatch, after_commit):
    calls = []

    async def forbidden_handler(request):
        calls.append(True)
        raise AssertionError("an unconfirmed claim must never execute")

    monkeypatch.setattr(http, "create_workspace", forbidden_handler)
    _fail_sqlite_commit(monkeypatch, commit_number=1, after_commit=after_commit)
    app = make_app(tmp_path)
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    with TestClient(app) as client:
        first = client.post(PROTECTED, json={"name": "one"}, headers=headers)
        assert first.status_code == 503
        assert first.json()["error"]["code"] == "idempotency_unavailable"
        assert "Idempotency-Status" not in first.headers
        assert client.get("/v1/workspaces", headers=AUTH).json() == {"workspaces": []}
        if after_commit:
            retry = client.post(PROTECTED, json={"name": "one"}, headers=headers)
            assert retry.status_code == 503
            assert retry.json()["error"]["code"] == "idempotency_unknown"
            assert "Idempotency-Status" not in retry.headers
    if after_commit:
        with TestClient(make_app(tmp_path)) as client:
            restarted = client.post(PROTECTED, json={"name": "one"}, headers=headers)
            assert restarted.json()["error"]["code"] == "idempotency_unknown"
    assert calls == []


def test_failure_reported_after_result_commit_replays_saved_success(tmp_path, monkeypatch):
    _fail_sqlite_commit(monkeypatch, commit_number=2, after_commit=True)
    app = make_app(tmp_path)
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    with TestClient(app) as client:
        first = client.post(PROTECTED, json={"name": "one"}, headers=headers)
        assert first.status_code == 503
        assert first.json()["error"]["code"] == "idempotency_unknown"
        assert "Idempotency-Status" not in first.headers
        retry = client.post(PROTECTED, json={"name": "one"}, headers=headers)
        assert retry.status_code == 201
        assert retry.json()["id"] == "one"
        assert retry.headers["Idempotency-Status"] == "completed"
        assert retry.headers["Idempotency-Replayed"] == "true"
        assert len(client.get("/v1/workspaces", headers=AUTH).json()["workspaces"]) == 1
    with TestClient(make_app(tmp_path)) as client:
        restarted = client.post(PROTECTED, json={"name": "one"}, headers=headers)
        assert restarted.status_code == 201
        assert restarted.content == retry.content
        assert restarted.headers["Idempotency-Replayed"] == "true"
        assert len(client.get("/v1/workspaces", headers=AUTH).json()["workspaces"]) == 1


def test_failed_response_save_never_reexecutes(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}

    def fail(*args):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(app.state.operation_store, "complete", fail)
    with TestClient(app) as client:
        first = client.post(PROTECTED, json={"name": "one"}, headers=headers)
        assert first.status_code == 503
        assert first.json()["error"]["code"] == "idempotency_unknown"
        assert "Idempotency-Status" not in first.headers
        second = client.post(PROTECTED, json={"name": "one"}, headers=headers)
        assert second.json()["error"]["code"] == "idempotency_unknown"
    with TestClient(make_app(tmp_path)) as client:
        restarted = client.post(PROTECTED, json={"name": "one"}, headers=headers)
        assert restarted.json()["error"]["code"] == "idempotency_unknown"
        assert len(client.get("/v1/workspaces", headers=AUTH).json()["workspaces"]) == 1


def test_handler_exception_keeps_claim_unknown(tmp_path, monkeypatch):
    calls = []

    async def failing(request):
        calls.append(True)
        raise RuntimeError("effect already happened")

    monkeypatch.setattr(http, "create_workspace", failing)
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    with TestClient(make_app(tmp_path)) as client:
        for _ in range(2):
            response = client.post(PROTECTED, json={"name": "one"}, headers=headers)
            assert response.json()["error"]["code"] == "idempotency_unknown"
    assert calls == [True]


def test_recorded_5xx_replays_without_repeating_partial_effect(tmp_path, monkeypatch):
    calls = []

    async def partial(request):
        calls.append(True)
        return Response(b'{"error":{"code":"partial"}}', status_code=500)

    monkeypatch.setattr(http, "create_workspace", partial)
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    with TestClient(make_app(tmp_path)) as client:
        first = client.post(PROTECTED, json={"name": "one"}, headers=headers)
        second = client.post(PROTECTED, json={"name": "one"}, headers=headers)
        assert first.status_code == second.status_code == 500
        assert second.content == first.content
        assert first.headers["Idempotency-Status"] == "completed"
        assert second.headers["Idempotency-Replayed"] == "true"
    assert calls == [True]


def test_oversized_response_keeps_claim_unknown(tmp_path, monkeypatch):
    calls = []

    async def oversized(request):
        calls.append(True)
        return Response(b"x" * (1024 * 1024 + 1))

    monkeypatch.setattr(http, "create_workspace", oversized)
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    with TestClient(make_app(tmp_path)) as client:
        for _ in range(2):
            response = client.post(PROTECTED, json={"name": "one"}, headers=headers)
            assert response.json()["error"]["code"] == "idempotency_unknown"
    assert calls == [True]


@pytest.mark.anyio
async def test_response_is_durable_before_delivery(tmp_path):
    app = make_app(tmp_path)
    key = str(uuid4())

    async def receive():
        return {"type": "http.request", "body": b'{"name":"one"}', "more_body": False}

    async def dropped_send(message):
        raise OSError("connection dropped before response delivery")

    scope = {
        "type": "http",
        "method": "POST",
        "path": PROTECTED,
        "scheme": "http",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer test-token"), (b"idempotency-key", key.encode())],
    }
    with pytest.raises(OSError):
        await app(scope, receive, dropped_send)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(make_app(tmp_path)), base_url="http://test"
    ) as client:
        replayed = await client.post(
            PROTECTED, content=b'{"name":"one"}', headers={**AUTH, "Idempotency-Key": key}
        )
        assert replayed.status_code == 201
        assert replayed.json()["id"] == "one"
        assert replayed.headers["Idempotency-Replayed"] == "true"
        listed = await client.get("/v1/workspaces", headers=AUTH)
        assert len(listed.json()["workspaces"]) == 1


def test_store_claims_are_atomic_across_instances_and_threads(tmp_path):
    stores = [OperationStore(tmp_path / "ledger") for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(lambda store: store.claim("key", "fingerprint"), stores))
    assert claims.count("claimed") == 1
    assert claims.count("unknown") == 7
    owner = stores[claims.index("claimed")]
    assert owner.claim("key", "fingerprint") == "in_progress"
    result = StoredResponse(202, b'{"turn_id":"one"}', [(b"content-type", b"application/json")])
    owner.complete("key", result)
    assert all(store.claim("key", "fingerprint") == result for store in stores)


def test_fingerprint_is_length_delimited_and_includes_method():
    assert request_fingerprint("POST", "/a", b"bc", b"d") != request_fingerprint(
        "POST", "/a", b"b", b"cd"
    )
    assert request_fingerprint("POST", "/a", b"", b"") != request_fingerprint(
        "DELETE", "/a", b"", b""
    )


def test_capability_on_unhandled_error(tmp_path):
    app = make_app(tmp_path)

    async def broken(request):
        raise RuntimeError("broken read")

    app.router.routes.append(Route("/broken", broken))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/broken")
        assert response.status_code == 500
        assert response.headers["X-Marim-Idempotency"] == "v1"


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["claim", "complete"])
async def test_locked_ledger_does_not_block_health(tmp_path, monkeypatch, stage):
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    locker = None

    async def endpoint(request):
        if stage == "complete":
            locker.execute("BEGIN IMMEDIATE")
        return Response(b"done")

    monkeypatch.setattr(http, "create_workspace", endpoint)
    app = make_app(tmp_path)
    store = app.state.operation_store
    # Create the actual ledger, then observe when the mutation reaches its
    # contended SQL statement. The competing connection holds a real write lock.
    store._connect().close()
    connect = store._connect

    def traced_connect():
        connection = connect()

        def trace(sql):
            marker = "BEGIN IMMEDIATE" if stage == "claim" else "UPDATE operations SET"
            if sql.startswith(marker):
                loop.call_soon_threadsafe(entered.set)

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(store, "_connect", traced_connect)
    locker = sqlite3.connect(tmp_path / "state/idempotency/operations.sqlite3")
    if stage == "claim":
        locker.execute("BEGIN IMMEDIATE")
    request = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            request = asyncio.create_task(
                client.post(PROTECTED, json={}, headers={**AUTH, "Idempotency-Key": str(uuid4())})
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            health = await client.get("/v1/health")
            assert health.status_code == 200
            # This must finish while SQLite is still waiting, not after its
            # one-second busy timeout has already stalled the whole event loop.
            assert not request.done()
            locker.rollback()
            assert (await request).status_code == 200
    finally:
        locker.close()
        if request is not None:
            await request


@pytest.mark.anyio
@pytest.mark.parametrize("method,after", [("claim", False), ("claim", True), ("complete", False)])
async def test_cancellation_drains_ledger_worker_before_releasing_request(
    tmp_path, monkeypatch, method, after
):
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    app = make_app(tmp_path)
    store = app.state.operation_store
    operation = getattr(store, method)

    def paused(*args):
        result = operation(*args) if after else None
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5), "test did not release the ledger worker"
        return result if after else operation(*args)

    monkeypatch.setattr(store, method, paused)
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(client.post(PROTECTED, json={"name": "one"}, headers=headers))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            request.cancel()
            await asyncio.sleep(0)
            request.cancel()
            await asyncio.sleep(0)
            assert (await client.get("/v1/health")).status_code == 200
            assert not request.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        retry = await client.post(PROTECTED, json={"name": "one"}, headers=headers)
        listed = (await client.get("/v1/workspaces", headers=AUTH)).json()["workspaces"]
        if method == "claim":
            assert retry.status_code == 503
            assert retry.json()["error"]["code"] == "idempotency_unknown"
            assert listed == []
        else:
            assert retry.status_code == 201
            assert retry.headers["Idempotency-Replayed"] == "true"
            assert len(listed) == 1


@pytest.mark.anyio
async def test_cancelled_duplicate_does_not_abandon_live_owner(tmp_path, monkeypatch):
    handler_entered = asyncio.Event()
    finish_handler = asyncio.Event()
    duplicate_entered = asyncio.Event()
    release_duplicate = threading.Event()
    loop = asyncio.get_running_loop()

    async def endpoint(request):
        handler_entered.set()
        await finish_handler.wait()
        return Response(b"done")

    monkeypatch.setattr(http, "create_workspace", endpoint)
    app = make_app(tmp_path)
    claim = app.state.operation_store.claim

    def paused_claim(*args):
        result = claim(*args)
        loop.call_soon_threadsafe(duplicate_entered.set)
        assert release_duplicate.wait(timeout=5), "test did not release the duplicate worker"
        return result

    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        owner = asyncio.create_task(client.post(PROTECTED, json={}, headers=headers))
        try:
            await asyncio.wait_for(handler_entered.wait(), timeout=5)
            monkeypatch.setattr(app.state.operation_store, "claim", paused_claim)
            duplicate = asyncio.create_task(client.post(PROTECTED, json={}, headers=headers))
            await asyncio.wait_for(duplicate_entered.wait(), timeout=5)
            duplicate.cancel()
            release_duplicate.set()
            with pytest.raises(asyncio.CancelledError):
                await duplicate
            retry = await client.post(PROTECTED, json={}, headers=headers)
            assert retry.json()["error"]["code"] == "idempotency_in_progress"
        finally:
            release_duplicate.set()
            finish_handler.set()
            assert (await owner).status_code == 200


@pytest.mark.anyio
async def test_cancelled_handler_cannot_reexecute(tmp_path, monkeypatch):
    entered = asyncio.Event()

    async def cancelled(request):
        entered.set()
        await asyncio.Future()
        return Response()

    monkeypatch.setattr(http, "create_workspace", cancelled)
    app = make_app(tmp_path)
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        first = asyncio.create_task(client.post(PROTECTED, json={"name": "one"}, headers=headers))
        await asyncio.wait_for(entered.wait(), timeout=5)
        duplicate = await client.post(PROTECTED, json={"name": "one"}, headers=headers)
        assert duplicate.json()["error"]["code"] == "idempotency_in_progress"
        assert duplicate.headers["Retry-After"] == "1"
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        after = await client.post(PROTECTED, json={"name": "one"}, headers=headers)
        assert after.json()["error"]["code"] == "idempotency_unknown"


@pytest.mark.anyio
async def test_concurrent_messages_enqueue_one_real_turn_and_approval_replays(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    entered = asyncio.Event()
    release = asyncio.Event()
    generated = asyncio.Event()
    model_calls = []

    async def stream(messages, info):
        model_calls.append(True)
        yield "done"
        generated.set()

    async def factory(workspace, session_id, mode, project_memory_root=None):
        entered.set()
        await release.wait()
        manager = SessionManager(workspace)
        model = FunctionModel(
            lambda messages, info: ModelResponse(parts=[TextPart(content="done")]),
            stream_function=stream,
        )
        return _make_harness(
            model, _make_deps(workspace), store=manager.store(session_id), manager=manager
        )

    app = make_app(tmp_path, factory)
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            await client.post("/v1/workspaces", json={"name": "one"}, headers=AUTH)
            session = await client.post(
                "/v1/workspaces/one/sessions", json={"name": "test"}, headers=AUTH
            )
            sid = session.json()["id"]
            base = f"{PROTECTED}/one/sessions/{sid}"
            first_task = asyncio.create_task(
                client.post(base + "/messages", json={"prompt": "hello"}, headers=headers)
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            duplicate = await client.post(
                base + "/messages", json={"prompt": "hello"}, headers=headers
            )
            assert duplicate.status_code == 503
            assert duplicate.json()["error"]["code"] == "idempotency_in_progress"
            release.set()
            first = await first_task
            assert first.status_code == 202
            replayed = await client.post(
                base + "/messages", json={"prompt": "hello"}, headers=headers
            )
            assert replayed.json()["turn_id"] == first.json()["turn_id"]
            assert replayed.headers["Idempotency-Replayed"] == "true"
            await asyncio.wait_for(generated.wait(), timeout=5)
            host = app.state.supervisor.peek("one", sid)
            for _ in range(100):
                if not host.busy:
                    break
                await asyncio.sleep(0.01)
            assert not host.busy
            assert model_calls == [True]
            # Park a real approval through the Harness's bound UI seam. Its
            # successful answer disappears from the host, so a second execution
            # would return 404; a durable replay must preserve the original 200.
            approve = host.harness.deps.ui.request_approval
            pending = asyncio.create_task(approve(ToolCallPart("edit_file", {}, "call-one")))
            await asyncio.sleep(0)
            aid = host.pending_asks()[0]["id"]
            answer_headers = {**AUTH, "Idempotency-Key": str(uuid4())}
            answered = await client.post(
                base + f"/asks/{aid}", json={"approve": True}, headers=answer_headers
            )
            assert answered.status_code == 200
            assert await pending is True
            replayed_answer = await client.post(
                base + f"/asks/{aid}", json={"approve": True}, headers=answer_headers
            )
            assert replayed_answer.status_code == 200
            assert replayed_answer.headers["Idempotency-Replayed"] == "true"
            assert host.pending_asks() == []
    finally:
        await app.state.supervisor.aclose()

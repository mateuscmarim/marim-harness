from __future__ import annotations

import asyncio
import json

import pytest

from marim_harness.codex.rpc import JsonRpcClient, RpcError


class FakeWriter:
    def __init__(self) -> None:
        self.lines: list[dict] = []

    def write(self, data: bytes) -> None:
        for raw in data.decode().split("\n"):
            if raw:
                self.lines.append(json.loads(raw))

    async def drain(self) -> None:
        return None


async def _noop_notification(method: str, params: dict) -> None:
    return None


async def _noop_request(method: str, params: dict) -> dict:
    return {}


def _client(**kw):
    reader = asyncio.StreamReader()
    writer = FakeWriter()
    client = JsonRpcClient(
        reader,
        writer,
        on_notification=kw.get("on_notification", _noop_notification),
        on_server_request=kw.get("on_server_request", _noop_request),
    )
    return reader, writer, client


def _feed(reader: asyncio.StreamReader, obj: dict) -> None:
    reader.feed_data((json.dumps(obj) + "\n").encode())


@pytest.mark.anyio
async def test_request_matches_response_by_id():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    fut = asyncio.create_task(client.request("thread/start", {"cwd": "/x"}))
    await asyncio.sleep(0)
    sent = writer.lines[0]
    assert sent["method"] == "thread/start" and sent["params"] == {"cwd": "/x"}
    assert "jsonrpc" not in sent
    _feed(reader, {"id": sent["id"], "result": {"thread": {"id": "t1"}}})
    assert await fut == {"thread": {"id": "t1"}}
    reader.feed_eof()
    await loop


@pytest.mark.anyio
async def test_error_response_raises_rpc_error():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    fut = asyncio.create_task(client.request("thread/resume", {"threadId": "nope"}))
    await asyncio.sleep(0)
    _feed(reader, {"id": writer.lines[0]["id"], "error": {"code": -32000, "message": "no thread"}})
    with pytest.raises(RpcError) as exc:
        await fut
    assert exc.value.code == -32000 and "no thread" in str(exc.value)
    reader.feed_eof()
    await loop


@pytest.mark.anyio
async def test_notification_dispatch_and_params_default():
    got: list[tuple[str, dict]] = []

    async def on_notification(method: str, params: dict) -> None:
        got.append((method, params))

    reader, writer, client = _client(on_notification=on_notification)
    loop = asyncio.create_task(client.run())
    _feed(reader, {"method": "turn/started", "params": {"threadId": "t1"}})
    _feed(reader, {"method": "initialized"})
    reader.feed_eof()
    await loop
    assert got == [("turn/started", {"threadId": "t1"}), ("initialized", {})]


@pytest.mark.anyio
async def test_server_request_is_answered_with_handler_result():
    async def on_server_request(method: str, params: dict) -> dict:
        assert method == "item/commandExecution/requestApproval"
        return {"decision": "decline"}

    reader, writer, client = _client(on_server_request=on_server_request)
    loop = asyncio.create_task(client.run())
    _feed(reader, {"id": "srv-1", "method": "item/commandExecution/requestApproval", "params": {}})
    await asyncio.sleep(0.01)
    reader.feed_eof()
    await loop
    assert writer.lines == [{"id": "srv-1", "result": {"decision": "decline"}}]


@pytest.mark.anyio
async def test_server_request_handler_exception_becomes_error_reply():
    async def on_server_request(method: str, params: dict) -> dict:
        raise RpcError(-32601, "unknown method")

    reader, writer, client = _client(on_server_request=on_server_request)
    loop = asyncio.create_task(client.run())
    _feed(reader, {"id": 7, "method": "mystery", "params": {}})
    await asyncio.sleep(0.01)
    reader.feed_eof()
    await loop
    assert writer.lines == [{"id": 7, "error": {"code": -32601, "message": "unknown method"}}]


@pytest.mark.anyio
async def test_eof_fails_pending_requests_and_sets_closed():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    fut = asyncio.create_task(client.request("model/list"))
    await asyncio.sleep(0)
    reader.feed_eof()
    await loop
    assert client.closed.is_set()
    with pytest.raises(RpcError, match="closed"):
        await fut


@pytest.mark.anyio
async def test_request_timeout():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    with pytest.raises(asyncio.TimeoutError):
        await client.request("model/list", timeout=0.01)
    reader.feed_eof()
    await loop


@pytest.mark.anyio
async def test_malformed_line_is_skipped():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    reader.feed_data(b"not json\n")
    _feed(reader, {"method": "warning", "params": {"message": "x"}})
    reader.feed_eof()
    await loop  # no exception

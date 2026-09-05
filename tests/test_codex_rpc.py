from __future__ import annotations

import asyncio
import json

import pytest

from marim_harness.codex.rpc import CLOSED_CODE, INTERNAL_ERROR_CODE, JsonRpcClient, RpcError


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


@pytest.mark.anyio
async def test_request_after_closed_raises_immediately_without_sending():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    reader.feed_eof()
    await loop
    assert client.closed.is_set()
    with pytest.raises(RpcError) as exc:
        await client.request("thread/start", {"cwd": "/x"})
    assert exc.value.code == CLOSED_CODE
    assert writer.lines == []  # never even attempted to write


@pytest.mark.anyio
async def test_notify_sends_a_method_only_message_with_no_id():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    await client.notify("turn/steer", {"input": [{"type": "text", "text": "hi"}]})
    await client.notify("turn/ack")  # no params -> the key must be omitted, not null
    reader.feed_eof()
    await loop
    assert writer.lines == [
        {"method": "turn/steer", "params": {"input": [{"type": "text", "text": "hi"}]}},
        {"method": "turn/ack"},
    ]
    assert "id" not in writer.lines[0] and "params" not in writer.lines[1]


@pytest.mark.anyio
async def test_blank_lines_are_skipped():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    reader.feed_data(b"\n")  # whitespace-only line: skipped before JSON parsing
    _feed(reader, {"method": "ping", "params": {}})
    reader.feed_eof()
    await loop  # no exception; the reader kept going past the blank line


@pytest.mark.anyio
async def test_non_dict_json_line_is_ignored():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    reader.feed_data(b"[1, 2, 3]\n")  # valid JSON, but not an object -> nothing to dispatch
    _feed(reader, {"method": "ping", "params": {}})
    reader.feed_eof()
    await loop  # no exception; the reader kept going past the non-dict line


@pytest.mark.anyio
async def test_a_dict_with_neither_id_nor_method_is_ignored():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    _feed(reader, {"unexpected": "shape"})  # neither a response, request, nor notification
    _feed(reader, {"method": "ping", "params": {}})
    reader.feed_eof()
    await loop  # _dispatch falls through all three arms without raising


@pytest.mark.anyio
async def test_response_for_an_unknown_id_is_silently_dropped():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    _feed(reader, {"id": 999, "result": {"unexpected": True}})  # no such pending request
    fut = asyncio.create_task(client.request("model/list"))
    await asyncio.sleep(0)
    _feed(reader, {"id": writer.lines[0]["id"], "result": {"ok": True}})
    assert await fut == {"ok": True}  # the real request still completes normally
    reader.feed_eof()
    await loop


@pytest.mark.anyio
async def test_a_future_already_done_when_run_exits_is_left_untouched():
    """`run()`'s EOF cleanup fails every future still pending — but a future
    that already has a result (e.g. its owning `request()` hasn't yet run its
    own `finally` to pop it) must not have that result clobbered."""
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    done_fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
    done_fut.set_result({"already": "done"})
    client._pending[123] = done_fut  # simulate the race directly
    reader.feed_eof()
    await loop
    assert done_fut.result() == {"already": "done"}  # untouched, not overwritten with an error


@pytest.mark.anyio
async def test_server_request_handler_plain_exception_becomes_internal_error_reply():
    async def on_server_request(method: str, params: dict) -> dict:
        raise ValueError("boom")

    reader, writer, client = _client(on_server_request=on_server_request)
    loop = asyncio.create_task(client.run())
    _feed(reader, {"id": 7, "method": "mystery", "params": {}})
    await asyncio.sleep(0.01)
    reader.feed_eof()
    await loop
    assert writer.lines == [{"id": 7, "error": {"code": INTERNAL_ERROR_CODE, "message": "boom"}}]


@pytest.mark.anyio
async def test_notification_handler_exception_does_not_stop_read_loop(caplog):
    """Notification handler exceptions must be logged but not kill the read loop."""

    async def on_notification(method: str, params: dict) -> None:
        if method == "turn/error":
            raise ValueError("handler crashed")

    reader, writer, client = _client(on_notification=on_notification)
    loop = asyncio.create_task(client.run())
    # Send a notification that raises
    _feed(reader, {"method": "turn/error", "params": {}})
    await asyncio.sleep(0.01)  # let handler task run
    # Send a request/response to verify the read loop is still alive
    fut = asyncio.create_task(client.request("thread/status"))
    await asyncio.sleep(0)
    _feed(reader, {"id": writer.lines[0]["id"], "result": {"ok": True}})
    assert await fut == {"ok": True}
    reader.feed_eof()
    await loop
    # Verify the exception was logged
    assert "handler crashed" in caplog.text
